import numpy as np
import deepinv as dinv
import hdf5storage
from pathlib import Path
import torch
from torch.utils.data import DataLoader
from deepinv.models.denoiser import Denoiser
from deepinv.optim.data_fidelity import L2
from deepinv.optim.optimizers import Optim
from deepinv.training_utils import test
from torchvision import datasets, transforms
from deepinv.utils.parameters import get_DPIR_params

# Setup paths for data loading, results and checkpoints.
ORIGINAL_DATA_DIR = Path("../datasets")
DATA_DIR = Path("../measurements")
RESULTS_DIR = Path("../results")
CKPT_DIR = Path("../checkpoints")
DEG_DIR = Path("../degradations")


# Set the global random seed from pytorch to ensure reproducibility of the example.
torch.manual_seed(0)


# Setup the variable to fetch dataset and operators.
denoiser_name = 'drunet'
dataset = 'set3c'
ckpt_path = CKPT_DIR / 'drunet_color.pth'
dataset_path = ORIGINAL_DATA_DIR / dataset
measurement_dir = DATA_DIR / dataset / 'deblur'


# Use parallel dataloader if using a GPU to fasten training, otherwise, as all computes are on CPU, use synchronous dataloading.
num_workers = 4 if torch.cuda.is_available() else 0


# Parameters of the algorithm to solve the inverse problem
n_images_max = 3
batch_size = 1
noise_level_img = 0.03
early_stop = False
train = False
img_size = 256
n_channels = 3 #3 for color images, 1 for gray-scale images


# Logging parameters
verbose = True
plot_metrics = True #compute performance and convergence metrics along the algorithm
wandb_vis = True #extract curves and images in Weight&Bias
plot_images = True #save images in RESULTS_DIR

# load opti
# mal parameters for DPIR 
lamb, sigma_denoiser, stepsize, max_iter = get_DPIR_params(noise_level_img)
params_algo = {'stepsize': stepsize, 'g_param': sigma_denoiser, 'lambda': lamb}


# Generate a motion blur operator.
kernel_index = 1 # which kernel to chose among the 8 motion kernels from 'Levin09.mat'
kernel_path = DEG_DIR / 'kernels' / 'Levin09.mat'
kernels = hdf5storage.loadmat(str(kernel_path))["kernels"]
filter_np = kernels[0, kernel_index].astype(np.float64)
filter_torch = torch.from_numpy(filter_np).unsqueeze(0).unsqueeze(0)
p = dinv.physics.BlurFFT(
    img_size=(n_channels, img_size, img_size),
    filter=filter_torch,
    device=dinv.device,
    noise_model=dinv.physics.GaussianNoise(sigma=noise_level_img),
)


# Select the data fidelity term
data_fidelity = L2()


# Specify the prior
model_spec = {
    'name': denoiser_name,
    'args': {
        'in_channels': n_channels+1,
        'out_channels': n_channels,
        'pretrained': ckpt_path,
        'train': False,
        'device': dinv.device
    }
}
prior = {'prox_g': Denoiser(model_spec)}


# Generate a dataset in a HDF5 folder in "{dir}/dinv_dataset0.h5'" and load it.
val_transform = transforms.Compose([transforms.ToTensor()])
dataset = datasets.ImageFolder(root=dataset_path, transform=val_transform)
generated_datasets_paths = dinv.datasets.generate_dataset(
    train_dataset=dataset,
    test_dataset=None,
    physics=p,
    device=dinv.device,
    save_dir=measurement_dir,
    max_datapoints=n_images_max,
    num_workers=num_workers
)
dataset = dinv.datasets.HDF5Dataset(path=generated_datasets_paths[0], train=True)
dataloader = DataLoader(
    dataset, batch_size=batch_size, num_workers=num_workers, shuffle=False
)


# isntanciate the algorithm class to solve the IP problem.
model = Optim(
    algo_name="HQS",
    prior=prior,
    data_fidelity=data_fidelity,
    early_stop=early_stop,
    max_iter=max_iter,
    verbose=verbose,
    params_algo=params_algo,
    return_metrics=plot_metrics,
)


# Evaluate the model on the problem.
test(
    model=model, 
    test_dataloader=dataloader,
    physics=p,
    device=dinv.device,
    plot_images=plot_images,
    plot_input=True,
    save_folder=str(RESULTS_DIR),
    plot_metrics=plot_metrics,
    verbose=verbose,
    wandb_vis=wandb_vis,
)

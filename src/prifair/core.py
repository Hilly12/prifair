"""Core API"""

from typing import Sequence, Union

import numpy as np
import torch
from opacus import GradSampleModule
from opacus.accountants import RDPAccountant
from opacus.accountants.utils import get_noise_multiplier
from opacus.optimizers import DPOptimizer
from opacus.privacy_engine import forbid_accumulation_hook
from opacus.utils.uniform_sampler import UniformWithReplacementSampler

from .data import NonUniformPoissonSampler, WeightedDataLoader
from .optimizers import DPSGDFOptimizer
from .utils import _data_loader_with_batch_sampler

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def reweigh(labels: Sequence[int]) -> np.ndarray:
    """Returns the inverse weighting for each sample in the dataset.

    Args:
        labels (Sequence[int]):
            The relevant sensitive group labels of the dataset.

    Returns:
        np.ndarray:
            The inverse weighting for each sample in the dataset.

    Usage:
        >>> reweigh([0, 1, 1, 2, 3])
        array([0.25, 0.125, 0.125, 0.25, 0.25])
    """

    num_samples = len(labels)

    sensitive_groups = []
    for value, counts in zip(*np.unique(labels, return_counts=True)):
        sensitive_groups.append((labels == value, counts))

    n_unique = len(sensitive_groups)
    target_prob = 1 / n_unique

    weights = np.zeros(num_samples)
    for mask, counts in sensitive_groups:
        weights[mask] = target_prob / counts

    return weights


def latent_reweigh(
    train_loader: torch.utils.data.DataLoader,
    vae: torch.nn.Module,
    alpha: float = 0.01,
    k: int = 16,
) -> np.ndarray:
    """Returns the inverse weighting for each sample in the dataset computed
    using the latent distributions.

    Args:
        train_loader (torch.utils.data.DataLoader):
            The relevant training data loader.
        vae (torch.nn.Module):
            The relevant VAE model.
        alpha (float):
            The hyperparameter for the latent space. Defaults to 0.01.
        k (int):
            The number of samples to use for the latent space. Defaults to 16.

    Returns:
        np.ndarray:
            The inverse weighting for each sample in the dataset.
    """

    dataloader = torch.utils.data.DataLoader(
        train_loader.dataset, batch_size=train_loader.batch_size, shuffle=False
    )

    mus = []
    for batch in dataloader:
        mu, _ = vae.encode(batch[0].to(device))
        mus.append(mu.cpu().detach().numpy())

    mu = np.concatenate(mus)

    weights = np.zeros(mu.shape[0])
    latent_dim = mu.shape[1]
    for i in range(latent_dim):
        hist, bin_edges = np.histogram(mu[:, i], density=True, bins=k)
        bin_edges[0] = float("-inf")
        bin_edges[-1] = float("inf")

        hist += alpha
        hist = hist / hist.sum()
        bin_idxs = np.digitize(mu[:, i], bin_edges)

        p = 1.0 / hist[bin_idxs - 1]
        p /= p.sum()

        weights = np.maximum(weights, p)

    weights /= weights.sum()

    return weights


def setup_weighted_dpsgd(
    data_loader: torch.utils.data.DataLoader,
    model: torch.nn.Module,
    optimizer: torch.optim.Optimizer,
    weights: Union[Sequence[float], np.ndarray],
    target_epsilon: float,
    target_delta: float,
    max_grad_norm: float,
    epochs: int,
):
    """Sets up the DP-SGD-W optimizer.

    Args:
        data_loader (torch.utils.data.DataLoader):
            The training data loader.
        model (torch.nn.Module):
            The model to be used during training.
        optimizer (torch.optim.Optimizer):
            The optimizer to be used during training.
        weights (Union[Sequence[float], np.ndarray]):
            The weights for each sample in the dataset.
        target_epsilon (float):
            The target epsilon for DP-SGD-W.
        target_delta (float):
            The target delta for DP-SGD-W.
        max_grad_norm (float):
            The gradient clipping bound for DP-SGD-W.
    """

    weights = np.array(weights)

    model = GradSampleModule(model)
    model.register_forward_pre_hook(forbid_accumulation_hook)

    N = len(data_loader.dataset)
    sample_rate = 1 / len(data_loader)
    max_sample_rate = np.max(weights) * N * sample_rate
    expected_batch_size = int(N * sample_rate)

    batch_sampler = NonUniformPoissonSampler(
        weights=weights, num_samples=N, sample_rate=sample_rate
    )
    dp_loader = _data_loader_with_batch_sampler(data_loader, batch_sampler, wrap=False)

    accountant = RDPAccountant()

    optimizer = DPOptimizer(
        optimizer=optimizer,
        noise_multiplier=get_noise_multiplier(
            target_epsilon=target_epsilon,
            target_delta=target_delta,
            sample_rate=max_sample_rate,
            steps=int(epochs / sample_rate),
            accountant=accountant.mechanism(),
        ),
        max_grad_norm=max_grad_norm,
        expected_batch_size=expected_batch_size,
    )

    optimizer.attach_step_hook(
        accountant.get_optimizer_hook_fn(sample_rate=sample_rate)
    )

    return dp_loader, model, optimizer, accountant


def setup_adaptive_clipped_dpsgd(
    data_loader: torch.utils.data.DataLoader,
    model: torch.nn.Module,
    optimizer: torch.optim.Optimizer,
    target_epsilon: float,
    target_delta: float,
    epochs: int,
    clipping: str = "dpsgdf",
    **kwargs
):
    """Sets up the DP-SGD-W optimizer.

    Args:
        data_loader (torch.utils.data.DataLoader):
            The training data loader.
        model (torch.nn.Module):
            The model to be used during training.
        optimizer (torch.optim.Optimizer):
            The optimizer to be used during training.
        target_epsilon (float):
            The target epsilon for DP-SGD-W.
        target_delta (float):
            The target delta for DP-SGD-W.
        max_grad_norm (float):
            The gradient clipping bound for DP-SGD-W.
        clipping (str):
            The clipping method to use. Takes values ["dpsgdf", "fairdp"].
            Defaults to "dpsgdf".
        **kwargs:
            Passed to the ``opacus.optimizers.DPOptimizer`` wrapper.
    """

    model = GradSampleModule(model)
    model.register_forward_pre_hook(forbid_accumulation_hook)

    N = len(data_loader.dataset)
    sample_rate = 1 / len(data_loader)
    expected_batch_size = int(N * sample_rate)

    batch_sampler = UniformWithReplacementSampler(
        num_samples=N, sample_rate=sample_rate
    )
    dp_loader = _data_loader_with_batch_sampler(data_loader, batch_sampler, wrap=True)

    accountant = RDPAccountant()

    if clipping == "dpsgdf":
        optimizer = DPSGDFOptimizer(
            optimizer=optimizer,
            noise_multiplier=get_noise_multiplier(
                target_epsilon=target_epsilon,
                target_delta=target_delta,
                sample_rate=sample_rate,
                steps=int(epochs / sample_rate),
                accountant=accountant.mechanism(),
            ),
            expected_batch_size=expected_batch_size,
            **kwargs
        )
    else:
        raise ValueError("``clipping`` must be one of ['dpsgdf', 'fairdp']")

    optimizer.attach_step_hook(
        accountant.get_optimizer_hook_fn(sample_rate=sample_rate)
    )

    return dp_loader, model, optimizer, accountant


def create_teacher_loaders(
    dataset: torch.utils.data.Dataset, n_teachers: int, batch_size: int
) -> Sequence[torch.utils.data.DataLoader]:
    teacher_loaders = []
    n_train = len(dataset)
    shuffled_idxs = np.random.permutation(n_train)
    size = n_train // n_teachers

    for i in range(n_teachers):
        idxs = shuffled_idxs[i * size : min((i + 1) * size, n_train)]
        subset_data = torch.utils.data.Subset(dataset, idxs)
        loader = torch.utils.data.DataLoader(
            subset_data, batch_size=batch_size, shuffle=True
        )
        teacher_loaders.append(loader)

    return teacher_loaders


def create_weighted_teacher_loaders(
    dataset: torch.utils.data.Dataset,
    n_teachers: int,
    batch_size: int,
    weights: Union[Sequence[float], np.ndarray, torch.Tensor],
) -> Sequence[WeightedDataLoader]:
    teacher_loaders = []
    n_train = len(dataset)
    shuffled_idxs = np.random.permutation(n_train)
    size = n_train // n_teachers

    for i in range(n_teachers):
        idxs = shuffled_idxs[i * size : min((i + 1) * size, n_train)]
        subset_data = torch.utils.data.Subset(dataset, idxs)
        loader = torch.utils.data.DataLoader(subset_data, batch_size=batch_size)
        weighted_loader = WeightedDataLoader(loader)
        weighted_loader.update_weights(weights[idxs])
        teacher_loaders.append(weighted_loader)

    return teacher_loaders


def laplacian_aggregator(
    teacher_preds: np.ndarray, target_epsilon: float
) -> np.ndarray:
    n_train_student = teacher_preds.shape[1]
    bins = teacher_preds.max() + 1
    label_counts = torch.zeros((n_train_student, bins), dtype=torch.long)
    for col in teacher_preds:
        label_counts[torch.arange(n_train_student), col] += 1

    # steps = int(epochs * len())
    beta = 1 / target_epsilon
    label_counts += np.random.laplace(0, beta, 1)
    labels = label_counts.argmax(dim=1)

    return labels

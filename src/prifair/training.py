"""Sample code for training models"""

from typing import Any, Mapping, Optional, Sequence, Tuple, Type

import numpy as np
import torch
from opacus import PrivacyEngine
from opacus.utils.batch_memory_manager import BatchMemoryManager
from opacus.validators import ModuleValidator
from tqdm import tqdm

from .data import IndexCachingBatchMemoryManager

from .core import (  # isort:skip
    latent_reweigh,
    reweigh,
    setup_adaptive_clipped_dpsgd,
    setup_weighted_dpsgd,
)

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def train_vanilla(
    train_loader: torch.utils.data.DataLoader,
    val_loader: Optional[torch.utils.data.DataLoader],
    model_class: Type[torch.nn.Module],
    optim_class: Type[torch.optim.Optimizer],
    loss_fn: torch.nn.Module,
    epochs: int,
    **kwargs,
) -> Tuple[torch.nn.Module, Mapping[str, Sequence[float]]]:
    """Train a model in the given environment.

    Args:
        train_loader (torch.utils.data.DataLoader):
            The training data loader.
        val_loader (Optional[torch.utils.data.DataLoader]):
            The validation data loader. If None, validation is not performed.
        model_class (Type[torch.nn.Module]):
            The class of the model to be used during training.
        optim_class (Type[torch.optim.Optimizer]):
            The class of the optimizer to be used during training.
        loss_fn (torch.nn.Module):
            The loss function.
        epochs (float):
            Number of epochs to train for.
        **kwargs:
            Passed to optim_class constructor.

    Returns:
        Tuple[torch.nn.Module, Mapping[str, Sequence[float]]]:
            The trained model and a dictionary consisting of train-time metrics.
    """

    model = model_class()
    model.to(device)

    train_loader = torch.utils.data.DataLoader(
        train_loader.dataset, batch_size=train_loader.batch_size, shuffle=True
    )

    criterion = loss_fn
    optimizer = optim_class(model.parameters(), **kwargs)

    model.train()
    losses = []
    accuracies = []

    for epoch in range(epochs):
        epoch_losses = []
        epoch_accuracies = []

        for batch in tqdm(train_loader):
            optimizer.zero_grad()

            images = batch[0].to(device)
            target = batch[1].to(device)

            output = model(images)
            loss = criterion(output, target)

            preds = np.argmax(output.detach().cpu().numpy(), axis=1)
            labels = target.detach().cpu().numpy()

            acc = (preds == labels).mean()

            epoch_losses.append(loss.item())
            epoch_accuracies.append(acc)

            loss.backward()
            optimizer.step()

        print(
            f"Train Epoch: {epoch + 1} "
            f"Loss: {np.mean(epoch_losses):.6f} "
            f"Acc@1: {np.mean(epoch_accuracies) * 100:.6f} "
        )

        losses.extend(epoch_losses)
        accuracies.extend(epoch_accuracies)

    metrics = {"loss": losses, "accuracy": accuracies}
    return model, metrics


def train_dpsgd(
    train_loader: torch.utils.data.DataLoader,
    val_loader: Optional[torch.utils.data.DataLoader],
    model_class: Type[torch.nn.Module],
    optim_class: Type[torch.optim.Optimizer],
    loss_fn: torch.nn.Module,
    target_epsilon: float,
    target_delta: float,
    max_grad_norm: float,
    epochs: int,
    max_physical_batch_size: int = 128,
    **kwargs,
) -> Tuple[torch.nn.Module, Mapping[str, Sequence[float]]]:
    """Train a model with DP-SGD in the given environment.

    Args:
        train_loader (torch.utils.data.DataLoader):
            The training data loader.
        val_loader (Optional[torch.utils.data.DataLoader]):
            The validation data loader. If None, validation is not performed.
        model_class (Type[torch.nn.Module]):
            The class of the model to be used during training.
        optim_class (Type[torch.optim.Optimizer]):
            The class of the optimizer to be used during training.
        loss_fn (torch.nn.Module):
            The loss function.
        target_epsilon (float):
            The target epsilon for DP-SGD.
        target_delta (float):
            The target delta for DP-SGD.
        max_grad_norm (float):
            Gradient clipping bound for DP-SGD.
        epochs (float):
            Number of epochs to train for.
        max_physical_batch_size (int, optional):
            Maximum physical batch size for memory manager. Defaults to 128.
        **kwargs:
            Passed to optim_class constructor.

    Returns:
        Tuple[torch.nn.Module, Mapping[str, Sequence[float]]]:
            The trained model and a dictionary consisting of train-time metrics.
    """

    model = model_class()
    model = ModuleValidator.fix(model)
    assert not ModuleValidator.validate(model, strict=False)

    model = model.to(device)

    criterion = loss_fn
    optimizer = optim_class(model.parameters(), **kwargs)
    privacy_engine = PrivacyEngine()

    model, optimizer, train_loader = privacy_engine.make_private_with_epsilon(
        module=model,
        optimizer=optimizer,
        data_loader=train_loader,
        epochs=epochs,
        target_epsilon=target_epsilon,
        target_delta=target_delta,
        max_grad_norm=max_grad_norm,
    )

    model.train()
    losses = []
    accuracies = []

    for epoch in range(epochs):
        epoch_losses = []
        epoch_accuracies = []

        with BatchMemoryManager(
            data_loader=train_loader,
            max_physical_batch_size=max_physical_batch_size,
            optimizer=optimizer,
        ) as memory_safe_data_loader:

            for batch in tqdm(memory_safe_data_loader):
                optimizer.zero_grad()

                images = batch[0].to(device)
                target = batch[1].to(device)

                output = model(images)
                loss = criterion(output, target)

                preds = np.argmax(output.detach().cpu().numpy(), axis=1)
                labels = target.detach().cpu().numpy()

                acc = (preds == labels).mean()

                epoch_losses.append(loss.item())
                epoch_accuracies.append(acc)

                loss.backward()
                optimizer.step()

            epsilon = privacy_engine.get_epsilon(target_delta)

        print(
            f"Train Epoch: {epoch + 1} "
            f"Loss: {np.mean(epoch_losses):.6f} "
            f"Acc@1: {np.mean(epoch_accuracies) * 100:.6f} "
            f"(ε = {epsilon:.2f}, δ = {target_delta})"
        )

        losses.extend(epoch_losses)
        accuracies.extend(epoch_accuracies)

    metrics = {"loss": losses, "accuracy": accuracies}
    return model, metrics


def train_dpsgd_weighted(
    train_loader: torch.utils.data.DataLoader,
    val_loader: Optional[torch.utils.data.DataLoader],
    model_class: Type[torch.nn.Module],
    optim_class: Type[torch.optim.Optimizer],
    loss_fn: torch.nn.Module,
    target_epsilon: float,
    target_delta: float,
    max_grad_norm: float,
    epochs: int,
    max_physical_batch_size: int = 128,
    weighting: str = "latent",
    vae: Optional[torch.nn.Module] = None,
    weights: Optional[np.ndarray] = None,
    labels: Optional[np.ndarray] = None,
    alpha: float = 0.01,
    k: int = 16,
    **kwargs,
) -> Tuple[torch.nn.Module, Mapping[str, Sequence[float]]]:
    """Train a model with DP-SGD-W in the given environment.

    Args:
        train_loader (torch.utils.data.DataLoader):
            The training data loader.
        val_loader (Optional[torch.utils.data.DataLoader]):
            The validation data loader. If None, validation is not performed.
        model_class (Type[torch.nn.Module]):
            The class of the model to be used during training.
        optim_class (Type[torch.optim.Optimizer]):
            The class of the optimizer to be used during training.
        loss_fn (torch.nn.Module):
            The loss function.
        target_epsilon (float):
            The target epsilon for DP-SGD-W.
        target_delta (float):
            The target delta for DP-SGD-W.
        max_grad_norm (float):
            Gradient clipping bound for DP-SGD-W.
        weighting (str):
            The scheme to use for weighting the data.
            Can be one of ["custom", "latent", "sensitive_attr"].
            If set to "custom", then the weights must be provided in the `weights`
            argument. If set to "latent", then the weights are computed using the VAE,
            which must be provided in the `vae` argument. If set to "sensitive_attr",
            then the weights are computed in order to rebalance the distribution of
            the labels, which must be provided in the `labels` argument.
            Defaults to "latent".
        epochs (int):
            The number of epochs to train for.
        max_physical_batch_size (int, optional):
            Maximum physical batch size for memory manager. Defaults to 128.
        vae (Optional[torch.nn.Module]):
            The VAE to use for weighting if weighting is set to "latent".
            Defaults to None.
        weights (np.ndarray, optional):
            The weights to use for the reweighing if weighting is set to "custom".
            Defaults to None.
        labels (np.ndarray, optional):
            The labels to use for the reweighing if weighting is set to "sensitive_attr".
        alpha (float):
            The weight smoothing parameter for latent reweighing if weighting is
            set to "latent". Defaults to 0.01.
        k (int):
            The number of latent bins for latent reweighing if weighing is set
            to "latent". Defaults to 16.
        **kwargs:
            Passed to optim_class constructor.

    Returns:
        Tuple[torch.nn.Module, Mapping[str, Sequence[float]]]:
            The trained model and a dictionary consisting of train-time metrics.
    """

    print("Reweighing...")

    if weighting == "latent":
        if vae is None:
            raise ValueError("vae cannot be None if weighting is 'latent'")

        weights = latent_reweigh(train_loader, vae, alpha=alpha, k=k)

    elif weighting == "sensitive_attr":
        if labels is None:
            raise ValueError("labels cannot be None if weighting is 'sensitive_attr'")

        weights = reweigh(labels)

    elif weighting != "custom":
        raise ValueError(
            "weighting must be one of ['latent', 'sensitive_attr', 'custom']"
        )

    model = model_class()
    model = ModuleValidator.fix(model)
    assert not ModuleValidator.validate(model, strict=False)

    model.to(device)

    criterion = loss_fn
    optimizer = optim_class(model.parameters(), **kwargs)

    train_loader, model, optimizer, accountant = setup_weighted_dpsgd(
        data_loader=train_loader,
        model=model,
        optimizer=optimizer,
        weights=weights,
        target_epsilon=target_epsilon,
        target_delta=target_delta,
        max_grad_norm=max_grad_norm,
        epochs=epochs,
    )

    model.train()
    losses = []
    accuracies = []

    print("Training Model...")

    for epoch in range(epochs):
        epoch_losses = []
        epoch_accuracies = []

        with BatchMemoryManager(
            data_loader=train_loader,
            max_physical_batch_size=max_physical_batch_size,
            optimizer=optimizer,
        ) as memory_safe_data_loader:

            for batch in tqdm(memory_safe_data_loader):
                optimizer.zero_grad()

                images = batch[0].to(device)
                target = batch[1].to(device)

                output = model(images)
                loss = criterion(output, target)

                preds = np.argmax(output.detach().cpu().numpy(), axis=1)
                labels = target.detach().cpu().numpy()

                acc = (preds == labels).mean()

                epoch_losses.append(loss.item())
                epoch_accuracies.append(acc)

                loss.backward()
                optimizer.step()

            epsilon = accountant.get_epsilon(target_delta)

        print(
            f"Train Epoch: {epoch + 1} "
            f"Loss: {np.mean(epoch_losses):.6f} "
            f"Acc@1: {np.mean(epoch_accuracies) * 100:.6f} "
            f"(ε = {epsilon:.2f}, δ = {target_delta})"
        )

        losses.extend(epoch_losses)
        accuracies.extend(epoch_accuracies)

    metrics = {"loss": losses, "accuracy": accuracies}
    return model, metrics


def train_pate(
    train_loader: torch.utils.data.DataLoader,
    student_loader: torch.utils.data.DataLoader,
    model_class: Type[torch.nn.Module],
    optim_class: Type[torch.optim.Optimizer],
    loss_fn: torch.nn.Module,
    n_teachers: int,
    target_epsilon: float,
    target_delta: float,
    epochs: int,
    **kwargs,
):
    """Train a model with PATE in the given environment.

    Args:
        train_loader (torch.utils.data.DataLoader):
            The training dataloader used to train the teacher ensemble model.
        student_loader (torch.utils.data.DataLoader):
            The training dataloader for the public data used to train the student model.
        model_class (Type[torch.nn.Module]):
            The class of the model to be used during training.
        optim_class (Type[torch.optim.Optimizer]):
            The class of the optimizer to be used during training.
        loss_fn (torch.nn.Module):
            The loss function.
        n_teachers (int):
            The number of teachers to use in the ensemble.
        target_epsilon (float):
            The target epsilon for DP-SGD-W.
        target_delta (float):
            The target delta for DP-SGD-W.
        epochs (int):
            The number of epochs to train for.
        **kwargs:
            Passed to optim_class constructor.
    """

    teacher_loaders = []
    n_train = len(train_loader.dataset)
    data_size = n_train // n_teachers
    for i in range(n_teachers):
        idxs = list(range(i * data_size, max((i + 1) * data_size, n_train)))
        subset_data = torch.utils.data.Subset(train_loader.dataset, idxs)
        loader = torch.utils.data.DataLoader(
            subset_data, batch_size=train_loader.batch_size, shuffle=True
        )
        teacher_loaders.append(loader)

    criterion = loss_fn
    teachers = []

    print(f"Training {n_teachers} Teacher Models...")

    for i in range(n_teachers):
        model = model_class()
        model.to(device)
        optimizer = optim_class(model.parameters(), **kwargs)

        model.train()

        for _ in tqdm(range(epochs)):
            epoch_losses = []
            epoch_accuracies = []

            for batch in teacher_loaders[i]:
                optimizer.zero_grad()

                images = batch[0].to(device)
                target = batch[1].to(device)

                output = model(images)
                loss = criterion(output, target)

                preds = np.argmax(output.detach().cpu().numpy(), axis=1)
                labels = target.detach().cpu().numpy()

                acc = (preds == labels).mean()

                epoch_losses.append(loss.item())
                epoch_accuracies.append(acc)

                loss.backward()
                optimizer.step()

        teachers.append(model.cpu())

        print(
            f"Teacher Model: {i + 1} "
            f"Loss: {np.mean(epoch_losses):.6f} "
            f"Acc@1: {np.mean(epoch_accuracies) * 100:.6f} "
        )

    print("Aggregating Teachers...")

    n_train_student = len(student_loader.dataset)
    preds = torch.zeros((n_teachers, n_train_student), dtype=torch.long)
    for i, model in enumerate(tqdm(teachers)):
        outputs = torch.zeros(0, dtype=torch.long)

        model.eval()
        for batch in student_loader:
            output = model(batch[0])
            probs = torch.argmax(output, dim=1)
            outputs = torch.cat((outputs, probs))

        preds[i] = outputs

    bins = preds.max() + 1
    label_counts = torch.zeros((n_train_student, bins), dtype=torch.long)
    for col in preds:
        label_counts[np.arange(n_train_student), col] += 1

    beta = 1 / target_epsilon
    label_counts += np.random.laplace(0, beta, 1)
    labels = label_counts.argmax(dim=1)

    def gen_student_loader(student_loader, labels):
        for i, batch in enumerate(iter(student_loader)):
            yield batch[0], labels[i * len(batch[0]) : (i + 1) * len(batch[0])]

    student_model = model_class()
    student_model.to(device)

    optimizer = optim_class(student_model.parameters(), **kwargs)

    student_model.train()
    losses = []
    accuracies = []

    print("Training Student Model...")

    for epoch in range(epochs):
        epoch_losses = []
        epoch_accuracies = []

        generator = gen_student_loader(student_loader, labels)
        for images, target in tqdm(generator, total=len(student_loader)):
            optimizer.zero_grad()

            images = images.to(device)
            target = target.to(device)

            output = student_model(images)
            loss = criterion(output, target)

            preds = np.argmax(output.detach().cpu().numpy(), axis=1)
            lbls = target.detach().cpu().numpy()

            acc = (preds == lbls).mean()

            epoch_losses.append(loss.item())
            epoch_accuracies.append(acc)

            loss.backward()
            optimizer.step()

        print(
            f"Train Epoch: {epoch + 1} "
            f"Loss: {np.mean(epoch_losses):.6f} "
            f"Acc@1: {np.mean(epoch_accuracies) * 100:.6f} "
        )

        losses.extend(epoch_losses)
        accuracies.extend(epoch_accuracies)

    metrics = {"student_loss": losses, "student_accuracy": accuracies}
    return student_model, metrics


def train_dpsgdf(
    train_loader: torch.utils.data.DataLoader,
    val_loader: Optional[torch.utils.data.DataLoader],
    model_class: Type[torch.nn.Module],
    optim_class: Type[torch.optim.Optimizer],
    loss_fn: torch.nn.Module,
    target_epsilon: float,
    target_delta: float,
    base_clipping_threshold: float,
    epochs: int,
    group_labels: torch.Tensor,
    max_physical_batch_size: int = 512,
    log_thresholds: bool = True,
    **kwargs,
) -> Tuple[torch.nn.Module, Mapping[str, Any]]:
    """Train a model with DPSGD-F in the given environment.
    Note that if the lot size is larger than `max_physical_batch_size`, the
    batch memory manager will split it into two lots which will be processed
    separately by the adaptive clipper.

    Args:
        train_loader (torch.utils.data.DataLoader):
            The training data loader.
        val_loader (Optional[torch.utils.data.DataLoader]):
            The validation data loader. If None, validation is not performed.
        model_class (Type[torch.nn.Module]):
            The class of the model to be used during training.
        optim_class (Type[torch.optim.Optimizer]):
            The class of the optimizer to be used during training.
        loss_fn (torch.nn.Module):
            The loss function.
        target_epsilon (float):
            The target epsilon for DP-SGD.
        target_delta (float):
            The target delta for DP-SGD.
        base_clipping_threshold (float):
            Base gradient clipping bound for DP-SGD.
        epochs (float):
            Number of epochs to train for.
        group_labels (torch.Tensor):
            The group labels for the data.
        max_physical_batch_size (int, optional):
            Maximum physical batch size for memory manager. Defaults to 128.
        log_thresholds (bool, optional):
            Logs the clipping thresholds at each iteration, storing them in class
            variable ``DPSGDFOptimizer.thresholds``. Defaults to True.
        **kwargs:
            Passed to optim_class constructor.

    Returns:
        Tuple[torch.nn.Module, Mapping[str, Any]]:
            The trained model and a dictionary consisting of train-time metrics.
    """

    model = model_class()
    model = ModuleValidator.fix(model)
    assert not ModuleValidator.validate(model, strict=False)

    model = model.to(device)

    criterion = loss_fn
    optimizer = optim_class(model.parameters(), **kwargs)

    n_groups = group_labels.max() + 1
    if not torch.all(torch.unique(group_labels) == torch.arange(n_groups)):
        raise ValueError(
            "Group labels must be unique and sequential starting from 0. \
            i.e. 0, 1, 2, ..."
        )

    train_loader, model, optimizer, accountant = setup_adaptive_clipped_dpsgd(
        data_loader=train_loader,
        model=model,
        optimizer=optimizer,
        target_epsilon=target_epsilon,
        target_delta=target_delta,
        base_clipping_threshold=base_clipping_threshold,
        epochs=epochs,
        n_groups=n_groups,
        log_thresholds=log_thresholds,
    )

    model.train()
    losses = []
    accuracies = []

    print("Training Model...")

    for epoch in range(epochs):
        epoch_losses = []
        epoch_accuracies = []

        with IndexCachingBatchMemoryManager(
            data_loader=train_loader,
            max_physical_batch_size=max_physical_batch_size,
            optimizer=optimizer,
        ) as memory_safe_data_loader:

            for batch in tqdm(memory_safe_data_loader):
                optimizer.zero_grad()

                idxs = memory_safe_data_loader.get_indices()
                batch_groups = group_labels[idxs].to(device)

                images = batch[0].to(device)
                target = batch[1].to(device)

                output = model(images)
                loss = criterion(output, target)

                preds = np.argmax(output.detach().cpu().numpy(), axis=1)
                labels = target.detach().cpu().numpy()

                acc = (preds == labels).mean()

                epoch_losses.append(loss.item())
                epoch_accuracies.append(acc)

                loss.backward()
                optimizer.step(group_labels=batch_groups)

        epsilon = accountant.get_epsilon(target_delta)

        print(
            f"Train Epoch: {epoch + 1} "
            f"Loss: {np.mean(epoch_losses):.6f} "
            f"Acc@1: {np.mean(epoch_accuracies) * 100:.6f} "
            f"(ε = {epsilon:.2f}, δ = {target_delta})"
        )

        losses.extend(epoch_losses)
        accuracies.extend(epoch_accuracies)

    metrics = {
        "loss": losses,
        "accuracy": accuracies,
        "thresholds": optimizer.thresholds,
    }
    return model, metrics

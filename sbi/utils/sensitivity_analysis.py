from copy import deepcopy
from typing import Optional
from warnings import warn

import numpy as np
import torch
import torch.nn as nn
from torch import optim
from torch.nn import MSELoss
from torch.utils import data
from torch.utils.data.sampler import SubsetRandomSampler

from sbi.utils.torchutils import get_default_device


def fit_regression_net(
    neural_net: torch.nn.Module,
    theta: torch.Tensor,
    label: torch.Tensor,
    num_train: int,
    batch_size: int = 100,
    learning_rate: float = 1e-4,
    validation_fraction: float = 0.1,
    stop_after_epochs: int = 20,
    max_epochs: int = 1000,
):
    """
    Trains a regression net with CrossEntropy loss.
    The classifier is used to distinguish between good and bad simulations.
    Bad simulations have e.g. NaN as summary stats
    Args:
        neural_net: torch.nn.Module. Obtained from get_nn_classifier.py
        theta: parameter set, sampled e.g. from prior or posterior, shape [batchsize x datashape]
        label: feature indicating if parameter lead to a bad simulation or not, shape [batchsize]
        num_train: number of training samples to use
        batch_size: size of minibatch
        learning_rate: learning rate of optimizer
        validation_fraction: fraction of samples used for validation
        stop_after_epochs: if validation loss does not decrease for stop_after_epochs epochs,
            we stop training
        subsample_bad_sims_factor: [0, 1]. One way to deal with imbalanced data (e.g. 99% bad simulations).
            Defines the fraction of bad simulations that will randomly be thrown out
        reweigh_loss: one way to deal with imbalanced data (e.g. 99% bad simulations).
            If True, we reweigh the CrossEntropyLoss such that it implicitly assigns equal prior
            weight to being a bad or good simulation.
        reweigh_factor: if reweigh_loss is True, but we want to reweigh to a custom prior weight,
            this can be done here. The value assigned will be the reweighing factor for bad simulations,
            (1-reweigh_factor) will be the factor for good simulations.
        function. Should take in summary stats x and output whether the stats are counted as good
            simulation (output 1.0) or as bad simulation (output 0.0). If None, we already expect a vector indicating
            the good/bad label. Other option is good_bad_criterion='NaN', which will treat simulations with at least one
            NaN as bad simulation and all others as good.
    Returns: trained network at minimum validation loss
    """

    train_loader, val_loader, num_validation_examples = data_loader(
        theta, label, num_train, batch_size, validation_fraction
    )

    optimizer = optim.Adam(list(neural_net.parameters()), lr=learning_rate,)

    model_parameters = filter(lambda p: p.requires_grad, neural_net.parameters())
    totalparams = sum([np.prod(p.size()) for p in model_parameters])
    print("Total number of trainable parameters", totalparams)

    # criterion / loss
    criterion = MSELoss()

    # Keep track of best_validation log_prob seen so far.
    best_validation_log_prob = 1e10
    best_net = neural_net
    # Keep track of number of epochs since last improvement.
    epochs_since_last_improvement = 0
    # Keep track of model with best validation performance.
    best_model_state_dict = None

    epochs = 0
    converged = False
    while not converged:

        # Train for a single epoch.
        neural_net.train()
        total_train_loss = 0
        for parameters, observations in train_loader:
            optimizer.zero_grad()
            outputs = neural_net(parameters)
            loss = criterion(outputs, observations)
            total_train_loss += loss.item()
            loss.backward()
            optimizer.step()
        total_train_loss /= num_train - num_validation_examples

        epochs += 1

        # calculate validation performance
        neural_net.eval()
        validation_loss = 0

        with torch.no_grad():
            for parameters, observations in val_loader:
                outputs = neural_net(parameters)
                loss = criterion(outputs, observations)
                validation_loss += loss.item()
        validation_loss /= num_validation_examples

        # check for improvement
        print("Training loss  ", total_train_loss)
        print("validation loss", validation_loss)
        if validation_loss < best_validation_log_prob:
            best_model_state_dict = deepcopy(neural_net.state_dict())
            best_net = neural_net
            best_validation_log_prob = validation_loss
            epochs_since_last_improvement = 0
        else:
            epochs_since_last_improvement += 1

        if epochs > max_epochs:
            neural_net.load_state_dict(best_model_state_dict)
            converged = True

        # if no validation improvement over many epochs, stop training
        if epochs_since_last_improvement > stop_after_epochs - 1:
            neural_net.load_state_dict(best_model_state_dict)
            converged = True

    return best_net


def data_loader(
    theta,
    stats,
    num_train: int,
    batch_size: int = 100,
    validation_fraction: float = 0.1,
):
    """
    Loads the data for training the classifier. Takes in the datasets of theta and label
    and builds torch dataloaders for training and validation.
    Args:
        theta: np.array, parameter set, sampled e.g. from prior or posterior, shape [batchsize x datashape]
        stats: np.array, containing summary stats, or directly the labels (ones or zeros indicating good or bad). Feature indicating if parameter lead to a bad simulation or not, shape [batchsize]
        num_train: number of training samples to use
        batch_size: size of minibatch
        validation_fraction: fraction of samples used for validation
    Returns: torch dataloaders for training and validation sets, and the number of samples in the validation set
    """

    # make them an array and get only the requested number of samples
    subsampled_theta = np.asarray(theta)[:num_train]
    subsampled_label = np.asarray(stats)[:num_train]

    # squeeze dimension from new_sample_stats such that it is just [batchsize] instead of [batchsize x 1]
    subsampled_label = subsampled_label

    # move to torch. Label needs to be dtype long
    device = get_default_device()
    subsampled_theta_torch = torch.from_numpy(subsampled_theta).float().to(device)
    subsampled_label_torch = torch.from_numpy(subsampled_label).float().to(device)

    # get indices for permutation of the data
    num_examples = len(subsampled_theta)
    permuted_indices = torch.randperm(num_examples)
    num_training_examples = int((1 - validation_fraction) * num_examples)
    num_validation_examples = num_examples - num_training_examples
    train_indices, val_indices = (
        permuted_indices[:num_training_examples],
        permuted_indices[num_training_examples:],
    )

    # # standardize theta with training data
    # train_data = subsampled_theta_torch[permuted_indices]
    # train_mean = train_data.mean(dim=0)
    # train_std = train_data.std(dim=0)
    # normalized_theta_torch = (subsampled_theta_torch - train_mean) / train_std
    #
    # # standardize x with training data
    # train_data = subsampled_label_torch[permuted_indices]
    # train_mean = train_data.mean(dim=0)
    # train_std = train_data.std(dim=0)
    # normalized_x_torch = (subsampled_label_torch - train_mean) / train_std

    # Dataset is shared for training and validation loaders.
    dataset = data.TensorDataset(subsampled_theta_torch, subsampled_label_torch)

    # Create neural_net and validation loaders using a subset sampler.
    train_loader = data.DataLoader(
        dataset,
        batch_size=batch_size,
        drop_last=True,
        sampler=SubsetRandomSampler(train_indices),
    )
    val_loader = data.DataLoader(
        dataset,
        batch_size=min(batch_size, num_examples - num_training_examples),
        shuffle=False,
        drop_last=True,
        sampler=SubsetRandomSampler(val_indices),
    )

    return train_loader, val_loader, num_validation_examples

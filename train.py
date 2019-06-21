import torch.nn.functional as F
import torch.optim as optim
import torch.nn
import argparse
import model
import data
import torch
import time
from pathlib import Path
import tqdm
import json
import torch.utils.data
import utils
import sklearn.preprocessing
import numpy as np
import random
import copy


tqdm.monitor_interval = 0

# Training settings
parser = argparse.ArgumentParser(description='Open Unmix Trainer')

# which target do we want to train?
parser.add_argument('--target', type=str, default='vocals',
                    help='source target for musdb')

parser.add_argument('--dataset', type=str, default="musdb",
                    choices=['musdb', 'aligned', 'unaligned', 'mixedsources'],
                    help='Name of the dataset.')

parser.add_argument('--root', type=str, help='root path of dataset')

# I/O Parameters
parser.add_argument('--seq-dur', type=float, default=5.0,
                    help='Duration of <=0.0 will result in the full audio')

parser.add_argument('--output', type=str, default="OSU",
                    help='provide output path base folder name')
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='disables CUDA training')

parser.add_argument('--epochs', type=int, default=1000, metavar='N',
                    help='number of epochs to train (default: 1000)')
parser.add_argument('--patience', type=int, default=20,
                    help='early stopping patience (default: 20)')
parser.add_argument('--batch_size', type=int, default=16,
                    help='batch size')
parser.add_argument('--lr', type=float, default=0.001,
                    help='learning rate, defaults to 1e-3')
parser.add_argument('--lr-decay-stepsize', type=int, default=60,
                    help='stepsize after lr will decay by `--lr-decay-gamma`')
parser.add_argument('--lr-decay-gamma', type=float, default=0.1,
                    help='gamma of learning rate scheduler decay')
parser.add_argument('--weight-decay', type=float, default=0.00001,
                    help='gamma of learning rate scheduler decay')
parser.add_argument('--seed', type=int, default=1, metavar='S',
                    help='random seed (default: 1)')

parser.add_argument('--nfft', type=int, default=4096,
                    help='STFT fft size and window size')
parser.add_argument('--nhop', type=int, default=1024,
                    help='STFT hop size')
parser.add_argument('--hidden-size', type=int, default=512,
                    help='hidden size parameter of FC bottleneck layers')
parser.add_argument('--bandwidth', type=int, default=15000,
                    help='maximum model bandwidth in herz')

parser.add_argument('--nb-channels', type=int, default=1,
                    help='set number of channels for model (1, 2)')
parser.add_argument('--quiet', action='store_true', default=False,
                    help='less verbose during training')

args, _ = parser.parse_known_args()

use_cuda = not args.no_cuda and torch.cuda.is_available()
dataloader_kwargs = {'num_workers': 4, 'pin_memory': True} if use_cuda else {}


# use jpg or npy
torch.manual_seed(args.seed)
random.seed(args.seed)

device = torch.device("cuda" if use_cuda else "cpu")

train_dataset, valid_dataset, args = data.load_datasets(parser, args)

# create output dir if not exist
target_path = Path(args.output, args.target)
target_path.mkdir(parents=True, exist_ok=True)

train_sampler = torch.utils.data.DataLoader(
    train_dataset, batch_size=args.batch_size, shuffle=True,
    **dataloader_kwargs
)
valid_sampler = torch.utils.data.DataLoader(
    valid_dataset, batch_size=1,
    **dataloader_kwargs
)

freqs = np.linspace(
    0, float(train_dataset.sample_rate) / 2, args.nfft // 2 + 1,
    endpoint=True
)

max_bin = np.max(np.where(freqs <= args.bandwidth)[0]) + 1
input_scaler = sklearn.preprocessing.StandardScaler()
output_scaler = sklearn.preprocessing.StandardScaler()
spec = torch.nn.Sequential(
    model.STFT(n_fft=args.nfft, n_hop=args.nhop),
    model.Spectrogram(mono=True)
)

train_dataset_scaler = copy.deepcopy(train_dataset)
train_dataset_scaler.samples_per_track = 1
train_dataset_scaler.augmentations = None
for x, y in tqdm.tqdm(train_dataset_scaler, disable=args.quiet):
    X = spec(x[None, ...])
    input_scaler.partial_fit(np.squeeze(X))

if not args.quiet:
    print("Computed global average spectrogram")

# set inital input scaler values
safe_input_scale = np.maximum(
    input_scaler.scale_,
    1e-4*np.max(input_scaler.scale_)
)

unmix = model.OpenUnmix(
    power=1,
    input_mean=input_scaler.mean_,
    input_scale=safe_input_scale,
    output_mean=None,
    nb_channels=args.nb_channels,
    hidden_size=args.hidden_size,
    n_fft=args.nfft,
    n_hop=args.nhop,
    max_bin=max_bin
).to(device)

optimizer = optim.Adam(
    unmix.parameters(),
    lr=args.lr,
    weight_decay=args.weight_decay
)
criterion = torch.nn.MSELoss()
scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
    optimizer,
    factor=args.lr_decay_gamma,
    patience=args.patience // 2
)


def train():
    losses = utils.AverageMeter()
    unmix.train()

    for x, y in tqdm.tqdm(train_sampler, disable=args.quiet):
        x, y = x.to(device), y.to(device)
        optimizer.zero_grad()
        Y_hat = unmix(x)
        Y = unmix.transform(y)
        loss = criterion(Y_hat, Y)
        loss.backward()
        optimizer.step()
        losses.update(loss.item())
    return losses.avg


def valid():
    losses = utils.AverageMeter()
    unmix.eval()
    with torch.no_grad():
        for x, y in valid_sampler:
            x, y = x.to(device), y.to(device)
            Y_hat = unmix(x)
            Y = unmix.transform(y)
            loss = F.mse_loss(Y_hat, Y)
            losses.update(loss.item(), Y.size(1))
        return losses.avg


es = utils.EarlyStopping(patience=args.patience)
t = tqdm.trange(1, args.epochs + 1, disable=args.quiet)
train_losses = []
valid_losses = []
train_times = []
best_epoch = 0

for epoch in t:
    end = time.time()
    train_loss = train()
    valid_loss = valid()
    scheduler.step(valid_loss)
    train_losses.append(train_loss)
    valid_losses.append(valid_loss)

    t.set_postfix(
        train_loss=train_loss, val_loss=valid_loss
    )

    stop = es.step(valid_loss)

    if valid_loss == es.best:
        best_epoch = epoch

    utils.save_checkpoint({
            'epoch': epoch + 1,
            'state_dict': unmix.state_dict(),
            'best_loss': es.best,
            'optimizer': optimizer.state_dict(),
        },
        is_best=valid_loss == es.best,
        path=target_path,
        target=args.target,
        model=unmix
    )

    # save params
    params = {
        'epochs_trained': epoch,
        'args': vars(args),
        'best_loss': es.best,
        'best_epoch': best_epoch,
        'train_loss_history': train_losses,
        'valid_loss_history': valid_losses,
        'train_time_history': train_times,
        'rate': train_dataset.sample_rate
    }

    with open(Path(target_path,  "output.json"), 'w') as outfile:
        outfile.write(json.dumps(params, indent=4, sort_keys=True))

    train_times.append(time.time() - end)

    if stop:
        print("Apply Early Stopping")
        break

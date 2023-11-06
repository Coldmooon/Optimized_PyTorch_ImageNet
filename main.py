import os
import random
import warnings

import torch
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.multiprocessing as mp
import torch.nn as nn
import torch.nn.parallel
import torch.optim
import torch.utils.data
import torch.utils.data.distributed
import torchvision.models as models

from torch.optim.lr_scheduler import MultiStepLR
from torch.optim.lr_scheduler import ChainedScheduler

import wandb

from opts import ArgumentParser
from datasets import dataloader as Dataloader
from train import Trainer
from checkpoints import Checkpoints

best_acc1 = 0
best_acc5 = 0

def main():
    arg_parser = ArgumentParser()
    args = arg_parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)
        torch.manual_seed(args.seed)
        cudnn.deterministic = True
        cudnn.benchmark = False
        warnings.warn('You have chosen to seed training. '
                      'This will turn on the CUDNN deterministic setting, '
                      'which can slow down your training considerably! '
                      'You may see unexpected behavior when restarting '
                      'from checkpoints.')

    if args.gpu is not None:
        warnings.warn('You have chosen a specific GPU. This will completely '
                      'disable data parallelism.')

    if args.dist_url == "env://" and args.world_size == -1:
        args.world_size = int(os.environ["WORLD_SIZE"])

    args.distributed = args.world_size > 1 or args.multiprocessing_distributed

    if torch.cuda.is_available():
        ngpus_per_node = torch.cuda.device_count()
    else:
        ngpus_per_node = 1
    if args.multiprocessing_distributed:
        # Since we have ngpus_per_node processes per node, the total world_size
        # needs to be adjusted accordingly
        args.world_size = ngpus_per_node * args.world_size
        # Use torch.multiprocessing.spawn to launch distributed processes: the
        # main_worker process function
        mp.spawn(main_worker, nprocs=ngpus_per_node, args=(ngpus_per_node, args))
    else:
        # Simply call main_worker function
        main_worker(args.gpu, ngpus_per_node, args)


def main_worker(gpu, ngpus_per_node, args):
    global best_acc1
    global best_acc5
    args.gpu = gpu

    if args.gpu is not None:
        print("Use GPU: {} for training".format(args.gpu))

    if args.distributed:
        if args.dist_url == "env://" and args.rank == -1:
            args.rank = int(os.environ["RANK"])
        if args.multiprocessing_distributed:
            # For multiprocessing distributed training, rank needs to be the
            # global rank among all the processes
            args.rank = args.rank * ngpus_per_node + gpu
        dist.init_process_group(backend=args.dist_backend, init_method=args.dist_url,
                                world_size=args.world_size, rank=args.rank)


    run = None
    if args.rank == 0:
        print("Rank %d running wandb", args.rank)
        run = wandb.init(
            project="pytorch.examples.ddp",
            config={
                    "architecture": args.arch,
                    "learning_rate": args.lr,
                    "label_smoothing": args.label_smoothing,
                    "epochs": args.epochs,
                    "batch_size": args.batch_size * args.world_size / ngpus_per_node,
                    "dataset": "ImageNet 2012",
                    "num_workers": args.workers,
                    "num_gpus": args.world_size,
                    "world-size": args.world_size / ngpus_per_node,
                    "compiled": args.compiled,
                    "mixed precision": 1, 
                   }
            )
    else:
        print("Rank %d not running wandb", args.rank)
        run = None

    # Check to see if local_rank is 0
    is_master = args.rank == 0
    do_log = run is not None

    # create model
    if args.pretrained:
        print("=> using pre-trained model '{}'".format(args.arch))
        model = models.__dict__[args.arch](pretrained=True)
    else:
        print("=> creating model '{}'".format(args.arch))
        model = models.__dict__[args.arch]()

    if not torch.cuda.is_available() and not torch.backends.mps.is_available():
        print('using CPU, this will be slow')
    elif args.distributed:
        # For multiprocessing distributed, DistributedDataParallel constructor
        # should always set the single device scope, otherwise,
        # DistributedDataParallel will use all available devices.
        if torch.cuda.is_available():
            if args.gpu is not None:
                print('args.gpu:::::::', args.gpu)
                torch.cuda.set_device(args.gpu)
                model.cuda(args.gpu)
                # When using a single GPU per process and per
                # DistributedDataParallel, we need to divide the batch size
                # ourselves based on the total number of GPUs of the current node.
                args.batch_size = int(args.batch_size / ngpus_per_node)
                args.workers = int((args.workers + ngpus_per_node - 1) / ngpus_per_node)
                if args.compiled == 1:
                    model = torch.compile(model)   

                s = torch.cuda.Stream()
                s.wait_stream(torch.cuda.current_stream())
                with torch.cuda.stream(s):
                    model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
                torch.cuda.current_stream().wait_stream(s)
                # model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
            else:
                model.cuda()

                if args.compiled == 1:
                    model = torch.compile(model)   

                # DistributedDataParallel will divide and allocate batch_size to all
                # available GPUs if device_ids are not set
                model = torch.nn.parallel.DistributedDataParallel(model)
    elif args.gpu is not None and torch.cuda.is_available():
        torch.cuda.set_device(args.gpu)
        model = model.cuda(args.gpu)
    elif torch.backends.mps.is_available():
        device = torch.device("mps")
        model = model.to(device)
    else:
        # DataParallel will divide and allocate batch_size to all available GPUs
        if args.arch.startswith('alexnet') or args.arch.startswith('vgg'):
            model.features = torch.nn.DataParallel(model.features)
            model.cuda()
        else:
            model = torch.nn.DataParallel(model).cuda()


    if torch.cuda.is_available():
        if args.gpu:
            device = torch.device('cuda:{}'.format(args.gpu))
        else:
            device = torch.device("cuda")
    elif torch.backends.mps.is_available():
        device = torch.device("mps")
    else:
        device = torch.device("cpu")

    # watch gradients only for rank 0
    if is_master:
        run.watch(model)

    # define loss function (criterion)
    criterion = nn.CrossEntropyLoss(label_smoothing=args.label_smoothing).to(device)
    
    # define optimizer
    optimizer = torch.optim.SGD(model.parameters(), args.lr,
                                momentum=args.momentum,
                                weight_decay=args.weight_decay)
    
    # define learning rate scheduler
    from warmup import WarmupLR
    # Create an instance of the warmup scheduler with the desired parameters
    warmup_scheduler = WarmupLR(optimizer, start_factor=0.1/args.lr, total_iters=5, verbose=True)
    # Create an instance of the step decay scheduler with the desired parameters
    step_scheduler = MultiStepLR(optimizer, milestones=[60, 120, 150, 190], gamma=0.1, verbose=True)
    # Create an instance of the chained scheduler that combines the two schedulers
    scheduler = ChainedScheduler([warmup_scheduler, step_scheduler])
    
    
    # optionally resume from a checkpoint
    checkpoints = Checkpoints(args)
    best_acc1 = checkpoints.resume(model, optimizer, scheduler)

    # define dataloader
    train_loader, val_loader = Dataloader.dataloader(args)

    # construct Trainer
    Training = Trainer(model, optimizer, criterion, scheduler, args)

    if args.evaluate:
        Training.validate(val_loader)
        return

    if do_log:
        run.log({"learning_rate": optimizer.param_groups[0]["lr"]})

    is_best = None
    for epoch in range(args.start_epoch, args.epochs):
        if args.distributed and args.disable_dali:
            # train_sampler.set_epoch(epoch)
            train_loader.sampler.set_epoch(epoch)

        # train for one epoch
        Training.train(train_loader, epoch, run)

        # evaluate on validation set
        acc1, acc5 = Training.validate(val_loader)
        
        scheduler.step()
        
        # remember best acc@1 and save checkpoint
        is_best = acc1 > best_acc1
        best_acc1 = max(acc1, best_acc1)
        best_acc5 = max(acc5, best_acc5)

        if do_log:
            run.log({"learning_rate": optimizer.param_groups[0]["lr"], "top1.val": best_acc1, "top5.val": best_acc5})

        if not args.multiprocessing_distributed or (args.multiprocessing_distributed
                and args.rank % ngpus_per_node == 0):
            checkpoints.save({
                'epoch': epoch + 1,
                'arch': args.arch,
                'state_dict': model.state_dict(),
                'best_acc1': best_acc1,
                'optimizer' : optimizer.state_dict(),
                'scheduler' : scheduler.state_dict()
            }, is_best)
    if is_master:
        run.finish()


if __name__ == '__main__':
    main()

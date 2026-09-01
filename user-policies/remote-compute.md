## Compute Environment

This setup runs on a shared Linux HPC cluster managed by Slurm. Use login nodes
only for lightweight development, inspection, formatting, fast checks, and
small focused unit tests.

A hostname containing `ln` is a login node. A hostname containing `gpu` is a
GPU compute node; `gpu40` conventionally identifies an NVIDIA A100 with 40 GB
and `gpu80` an NVIDIA A100 with 80 GB. If the hostname does not match these
conventions, treat the node type and permitted workload as unknown. The
hostname describes the machine but does not authorize compute or Slurm work.

Some filesystems and mounts may be visible only on compute nodes. If a path is
missing or inaccessible on a login node, report the node and path and consider
node-specific visibility. Do not conclude that the path was deleted or start a
job merely to inspect it.

Do not run or submit Slurm jobs or other heavy computation unless explicitly
requested in the current prompt. This includes `sbatch`, `srun`, `salloc`,
`scancel`, training, distributed or multi-GPU work, large inference, broad test
suites, large data scans, and commands consuming substantial CPU, RAM, or
filesystem I/O. Without that authorization, prepare the command or script for
the user to run.

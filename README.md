<!--
 Copyright 2023 Google LLC

 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

      https://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
 -->

# Overview

xpk (Accelerated Processing Kit, pronounced x-p-k,) is a software tool to help
Cloud developers to orchestrate training jobs on accelerators such as TPUs and
GPUs on GKE. xpk handles the "multihost pods" of TPUs, GPUs (HGX H100) and CPUs
(n2-standard-32) as first class citizens.

xpk decouples provisioning capacity from running jobs. There are two structures:
clusters (provisioned VMs) and workloads (training jobs). Clusters represent the
physical resources you have available. Workloads represent training jobs -- at
any time some of these will be completed, others will be running and some will
be queued, waiting for cluster resources to become available.

The ideal workflow starts by provisioning the clusters for all of the ML
hardware you have reserved. Then, without re-provisioning, submit jobs as
needed. By eliminating the need for re-provisioning between jobs, using Docker
containers with pre-installed dependencies and cross-ahead of time compilation,
these queued jobs run with minimal start times. Further, because workloads
return the hardware back to the shared pool when they complete, developers can
achieve better use of finite hardware resources. And automated tests can run
overnight while resources tend to be underutilized.

xpk supports the following TPU types:
* v4
* v5e
* v5p

and the following GPU types:
* a100
* h100

and the following CPU types:
* n2-standard-32

# Installation
To install xpk, run the following command:

```shell
pip install xpk
```

# XPK for Large Scale (>1k VMs)

Follow user instructions in [xpk-large-scale-guide.sh](xpk-large-scale-guide.sh)
to use xpk for a GKE cluster greater than 1000 VMs. Run these steps to set up a
GKE cluster with large scale training and high throughput support with XPK, and
run jobs with XPK. We recommend you manually copy commands per step and verify
the outputs of each step.

# Example usages:

To get started, be sure to set your GCP Project and Zone as usual via `gcloud
config set`.

Below are reference commands. A typical journey starts with a `Cluster Create`
followed by many `Workload Create`s. To understand the state of the system you
might want to use `Cluster List` or `Workload List` commands. Finally, you can
cleanup with a `Cluster Delete`.

## Cluster Create

First set the project and zone through gcloud config or xpk arguments.

```shell
PROJECT_ID=my-project-id
ZONE=us-east5-b
# gcloud config:
gcloud config set project $PROJECT_ID
gcloud config set compute/zone $ZONE
# xpk arguments
xpk .. --zone $ZONE --project $PROJECT_ID
```


The cluster created is a regional cluster to enable the GKE control plane across
all zones.

*   Cluster Create (provision reserved capacity):

    ```shell
    # Find your reservations
    gcloud compute reservations list --project=$PROJECT_ID
    # Run cluster create with reservation.
    python3 xpk.py cluster create \
    --cluster xpk-test --tpu-type=v5litepod-256 \
    --num-slices=2 \
    --reservation=$RESERVATION_ID
    ```

*   Cluster Create (provision on-demand capacity):

    ```shell
    python3 xpk.py cluster create \
    --cluster xpk-test --tpu-type=v5litepod-16 \
    --num-slices=4 --on-demand
    ```

*   Cluster Create (provision spot / preemptable capacity):

    ```shell
    python3 xpk.py cluster create \
    --cluster xpk-test --tpu-type=v5litepod-16 \
    --num-slices=4 --spot
    ```

*   Cluster Create can be called again with the same `--cluster name` to modify
    the number of slices or retry failed steps.

    For example, if a user creates a cluster with 4 slices:

    ```shell
    python3 xpk.py cluster create \
    --cluster xpk-test --tpu-type=v5litepod-16 \
    --num-slices=4  --reservation=$RESERVATION_ID
    ```

    and recreates the cluster with 8 slices. The command will rerun to create 4
    new slices:

    ```shell
    python3 xpk.py cluster create \
    --cluster xpk-test --tpu-type=v5litepod-16 \
    --num-slices=8  --reservation=$RESERVATION_ID
    ```

    and recreates the cluster with 6 slices. The command will rerun to delete 2
    slices. The command will warn the user when deleting slices.
    Use `--force` to skip prompts.

    ```shell
    python3 xpk.py cluster create \
    --cluster xpk-test --tpu-type=v5litepod-16 \
    --num-slices=6  --reservation=$RESERVATION_ID

    # Skip delete prompts using --force.

    python3 xpk.py cluster create --force \
    --cluster xpk-test --tpu-type=v5litepod-16 \
    --num-slices=6  --reservation=$RESERVATION_ID

    ```
## Cluster Delete
*   Cluster Delete (deprovision capacity):

    ```shell
    python3 xpk.py cluster delete \
    --cluster xpk-test
    ```
## Cluster List
*   Cluster List (see provisioned capacity):

    ```shell
    python3 xpk.py cluster list
    ```
## Cluster Describe
*   Cluster Describe (see capacity):

    ```shell
    python3 xpk.py cluster describe \
    --cluster xpk-test
    ```

## Cluster Cacheimage
*   Cluster Cacheimage (enables faster start times):

    ```shell
    python3 xpk.py cluster cacheimage \
    --cluster xpk-test --docker-image gcr.io/your_docker_image \
    --tpu-type=v5litepod-16
    ```

## Workload Create
*   Workload Create (submit training job):

    ```shell
    python3 xpk.py workload create \
    --workload xpk-test-workload --command "echo goodbye" --cluster \
    xpk-test --tpu-type=v5litepod-16
    ```

### Set `max-restarts` for production jobs

* `--max-restarts <value>`: By default, this is 0. This will restart the job ""
times when the job terminates. For production jobs, it is recommended to
increase this to a large number, say 50. Real jobs can be interrupted due to
hardware failures and software updates. We assume your job has implemented
checkpointing so the job restarts near where it was interrupted.

### Workload Priority and Preemption
* Set the priority level of your workload with `--priority=LEVEL`

  We have five priorities defined: [`very-low`, `low`, `medium`, `high`, `very-high`].
  The default priority is `medium`.

  Priority determines:

  1. Order of queued jobs.

      Queued jobs are ordered by
      `very-low` < `low` < `medium` < `high` <  `very-high`

  2. Preemption of lower priority workloads.

      A higher priority job will `evict` lower priority jobs.
      Evicted jobs are brought back to the queue and will re-hydrate appropriately.

  #### General Example:
  ```shell
  python3 xpk.py workload create \
  --workload xpk-test-medium-workload --command "echo goodbye" --cluster \
  xpk-test --tpu-type=v5litepod-16 --priority=medium
  ```

## Workload Delete
*   Workload Delete (delete training job):

    ```shell
    python3 xpk.py workload delete \
    --workload xpk-test-workload --cluster xpk-test
    ```

    This will only delete `xpk-test-workload` workload in `xpk-test` cluster.

*   Workload Delete (delete all training jobs in the cluster):

    ```shell
    python3 xpk.py workload delete \
    --cluster xpk-test
    ```

    This will delete all the workloads in `xpk-test` cluster. Deletion will only begin if you type `y` or `yes` at the prompt.

*   Workload Delete supports filtering. Delete a portion of jobs that match user criteria.
    * Filter by Job: `filter-by-job`

    ```shell
    python3 xpk.py workload delete \
    --cluster xpk-test --filter-by-job=$USER
    ```

    This will delete all the workloads in `xpk-test` cluster whose names start with `$USER`. Deletion will only begin if you type `y` or `yes` at the prompt.

    * Filter by Status: `filter-by-status`

    ```shell
    python3 xpk.py workload delete \
    --cluster xpk-test --filter-by-status=QUEUED
    ```
    
    This will delete all the workloads in `xpk-test` cluster that have the status as Admitted or Evicted, and the number of running VMs is 0. Deletion will only begin if you type `y` or `yes` at the prompt. Status can be: `EVERYTHING`,`FINISHED`, `RUNNING`, `QUEUED`, `FAILED`, `SUCCESSFUL`.

## Workload List
*   Workload List (see training jobs):

    ```shell
    python3 xpk.py workload list \
    --cluster xpk-test
    ```

* Example Workload List Output:

  The below example shows four jobs of different statuses:

  * `user-first-job-failed`: **filter-status** is `FINISHED` and `FAILED`.
  * `user-second-job-success`: **filter-status** is `FINISHED` and `SUCCESSFUL`.
  * `user-third-job-running`: **filter-status** is `RUNNING`.
  * `user-forth-job-in-queue`: **filter-status** is `QUEUED`.
  * `user-fifth-job-in-queue-preempted`: **filter-status** is `QUEUED`.

  ```
  Jobset Name                     Created Time           Priority   TPU VMs Needed   TPU VMs Running/Ran   TPU VMs Done      Status     Status Message                                                  Status Time
  user-first-job-failed           2023-1-1T1:00:00Z      medium     4                4                     <none>            Finished   JobSet failed                                                   2023-1-1T1:05:00Z
  user-second-job-success         2023-1-1T1:10:00Z      medium     4                4                     4                 Finished   JobSet finished successfully                                    2023-1-1T1:14:00Z
  user-third-job-running          2023-1-1T1:15:00Z      medium     4                4                     <none>            Admitted   Admitted by ClusterQueue cluster-queue                          2023-1-1T1:16:00Z
  user-forth-job-in-queue         2023-1-1T1:16:05Z      medium     4                <none>                <none>            Admitted   couldn't assign flavors to pod set slice-job: insufficient unused quota for google.com/tpu in flavor 2xv4-8, 4 more need   2023-1-1T1:16:10Z
  user-fifth-job-preempted        2023-1-1T1:10:05Z      low        4                <none>                <none>            Evicted    Preempted to accommodate a higher priority Workload             2023-1-1T1:10:00Z
  ```

* Workload List supports filtering. Observe a portion of jobs that match user criteria.

  * Filter by Status: `filter-by-status`

  Filter the workload list by the status of respective jobs.
  Status can be: `EVERYTHING`,`FINISHED`, `RUNNING`, `QUEUED`, `FAILED`, `SUCCESSFUL`

  * Filter by Job: `filter-by-job`

  Filter the workload list by the name of a job.

    ```shell
    python3 xpk.py workload list \
    --cluster xpk-test --filter-by-job=$USER
    ```


## GPU usage

In order to use XPK for GPU, you can do so by using `device-type` flag.

*   Cluster Create (provision reserved capacity):

    ```shell
    # Find your reservations
    gcloud compute reservations list --project=$PROJECT_ID

    # Run cluster create with reservation.
    python3 xpk.py cluster create \
    --cluster xpk-test --device-type=h100-80gb-8 \
    --num-slices=2 \
    --reservation=$RESERVATION_ID
    ```

*   [Install NVIDIA GPU device drivers](https://cloud.google.com/container-optimized-os/docs/how-to/run-gpus#install)
    ```shell
    # List available driver versions
    gcloud compute ssh $NODE_NAME --command "sudo cos-extensions list"

    # Install the default driver
    gcloud compute ssh $NODE_NAME --command "sudo cos-extensions install gpu"
    # OR install a specific version of the driver
    gcloud compute ssh $NODE_NAME --command "sudo cos-extensions install gpu -- -version=DRIVER_VERSION"
    ```

*   Run a workload:

    ```shell
    # Submit a workload
    python3 xpk.py workload create \
    --cluster xpk-test --device-type h100-80gb-8 \
    --workload xpk-test-workload \
    --command="echo hello world"
    ```

## CPU usage

In order to use XPK for CPU, you can do so by using `device-type` flag.

*   Cluster Create (provision on-demand capacity):

    ```shell
    # Run cluster create with on demand capacity.
    python3 xpk.py cluster create \
    --cluster xpk-test \
    --device-type=n2-standard-32-256 \
    --num-slices=1 \
    --default-pool-cpu-machine-type=n2-standard-32 \
    --on-demand
    ```
    Note that `device-type` for CPUs is of the format <cpu-machine-type>-<number of VMs>, thus in the above example, user requests for 256 VMs of type n2-standard-32.
    Currently workloads using < 1000 VMs are supported.

*   Run a workload:

    ```shell
    # Submit a workload
    python3 xpk.py workload create \
    --cluster xpk-test \
    --num-slices=1 \
    --device-type=n2-standard-32-256 \
    --workload xpk-test-workload \
    --command="echo hello world"
    ```


# How to add docker images to a xpk workload

The default behavior is `xpk workload create` will layer the local directory (`--script-dir`) into
the base docker image (`--base-docker-image`) and run the workload command.
If you don't want this layering behavior, you can directly use `--docker-image`. Do not mix arguments from the two flows in the same command.

## Recommended / Default Docker Flow: `--base-docker-image` and `--script-dir`
This flow pulls the `--script-dir` into the `--base-docker-image` and runs the new docker image.

* The below arguments are optional by default. xpk will pull the local
  directory with a generic base docker image.

  - `--base-docker-image` sets the base image that xpk will start with.

  - `--script-dir` sets which directory to pull into the image. This defaults to the current working directory.

  See `python3 xpk.py workload create --help` for more info.

* Example with defaults which pulls the local directory into the base image:
  ```shell
  echo -e '#!/bin/bash \n echo "Hello world from a test script!"' > test.sh
  python3 xpk.py workload create --cluster xpk-test \
  --workload xpk-test-workload-base-image --command "bash test.sh" \
  --tpu-type=v5litepod-16 --num-slices=1
  ```

* Recommended Flow For Normal Sized Jobs (fewer than 10k accelerators):
  ```shell
  python3 xpk.py workload create --cluster xpk-test \
  --workload xpk-test-workload-base-image --command "bash custom_script.sh" \
  --base-docker-image=gcr.io/your_dependencies_docker_image \
  --tpu-type=v5litepod-16 --num-slices=1
  ```

## Optional Direct Docker Image Configuration: `--docker-image`
If a user wants to directly set the docker image used and not layer in the
current working directory, set `--docker-image` to the image to be use in the
workload.

* Running with `--docker-image`:
  ```shell
  python3 xpk.py workload create --cluster xpk-test \
  --workload xpk-test-workload-base-image --command "bash test.sh" \
  --tpu-type=v5litepod-16 --num-slices=1 --docker-image=gcr.io/your_docker_image
  ```

* Recommended Flow For Large Sized Jobs (more than 10k accelerators):
  ```shell
  python3 xpk.py cluster cacheimage \
  --cluster xpk-test --docker-image gcr.io/your_docker_image
  # Run workload create with the same image.
  python3 xpk.py workload create --cluster xpk-test \
  --workload xpk-test-workload-base-image --command "bash test.sh" \
  --tpu-type=v5litepod-16 --num-slices=1 --docker-image=gcr.io/your_docker_image
  ```

# More advanced facts:

* Workload create accepts a --env-file flag to allow specifying the container's
environment from a file. Usage is the same as Docker's
[--env-file flag](https://docs.docker.com/engine/reference/commandline/run/#env)

    Example File:
    ```shell
    LIBTPU_INIT_ARGS=--my-flag=true --performance=high
    MY_ENV_VAR=hello
    ```

* Workload create accepts a --debug-dump-gcs flag which is a path to GCS bucket.
Passing this flag sets the XLA_FLAGS='--xla_dump_to=/tmp/xla_dump/' and uploads
hlo dumps to the specified GCS bucket for each worker.


# Troubleshooting

## `Invalid machine type` for CPUs.
XPK will create a regional GKE cluster. If you see issues like

```shell
Invalid machine type e2-standard-32 in zone $ZONE_NAME
```

Please select a CPU type that exists in all zones in the region.

```shell
# Find CPU Types supported in zones.
gcloud compute machine-types list --zones=$ZONE_LIST
# Adjust default cpu machine type.
python3 xpk.py cluster create --default-pool-cpu-machine-type=CPU_TYPE ...
```

## Permission Issues: `requires one of ["permission_name"] permission(s)`.

1) Determine the role needed based on the permission error:

    ```shell
    # For example: `requires one of ["container.*"] permission(s)`
    # Add [Kubernetes Engine Admin](https://cloud.google.com/iam/docs/understanding-roles#kubernetes-engine-roles) to your user.
    ```

2) Add the role to the user in your project.

    Go to [iam-admin](https://console.cloud.google.com/iam-admin/) or use gcloud cli:
    ```shell
    PROJECT_ID=my-project-id
    CURRENT_GKE_USER=$(gcloud config get account)
    ROLE=roles/container.admin  # container.admin is the role needed for Kubernetes Engine Admin
    gcloud projects add-iam-policy-binding $PROJECT_ID --member user:$CURRENT_GKE_USER --role=$ROLE
    ```

3) Check the permissions are correct for the users.

    Go to [iam-admin](https://console.cloud.google.com/iam-admin/) or use gcloud cli:

    ```shell
    PROJECT_ID=my-project-id
    CURRENT_GKE_USER=$(gcloud config get account)
    gcloud projects get-iam-policy $PROJECT_ID --filter="bindings.members:$CURRENT_GKE_USER" --flatten="bindings[].members"
    ```

4) Confirm you have logged in locally with the correct user.

    ```shell
    gcloud auth login
    ```

### Roles needed based on permission errors:

* `requires one of ["container.*"] permission(s)`

  Add [Kubernetes Engine Admin](https://cloud.google.com/iam/docs/understanding-roles#kubernetes-engine-roles) to your user.

* `ERROR: (gcloud.monitoring.dashboards.list) User does not have permission to access projects instance (or it may not exist)`

  Add [Monitoring Viewer](https://cloud.google.com/iam/docs/understanding-roles#monitoring.viewer) to your user.


## Reservation Troubleshooting:

### How to determine your reservation and its size / utilization:

```shell
PROJECT_ID=my-project
ZONE=us-east5-b
RESERVATION=my-reservation-name
# Find the reservations in your project
gcloud beta compute reservations list --project=$PROJECT_ID
# Find the tpu machine type and current utilization of a reservation.
gcloud beta compute reservations describe $RESERVATION --project=$PROJECT_ID --zone=$ZONE
```

# TPU Workload Debugging

## Collect Stack Traces
[cloud-tpu-diagnostics](https://pypi.org/project/cloud-tpu-diagnostics/) PyPI package can be used to generate stack traces for workloads running in GKE. This package dumps the Python traces when a fault such as segmentation fault, floating-point exception, or illegal operation exception occurs in the program. Additionally, it will also periodically collect stack traces to help you debug situations when the program is unresponsive. You must make the following changes in the docker image running in a Kubernetes main container to enable periodic stack trace collection.
```shell
# main.py

from cloud_tpu_diagnostics import diagnostic
from cloud_tpu_diagnostics.configuration import debug_configuration
from cloud_tpu_diagnostics.configuration import diagnostic_configuration
from cloud_tpu_diagnostics.configuration import stack_trace_configuration

stack_trace_config = stack_trace_configuration.StackTraceConfig(
                      collect_stack_trace = True,
                      stack_trace_to_cloud = True)
debug_config = debug_configuration.DebugConfig(
                stack_trace_config = stack_trace_config)
diagnostic_config = diagnostic_configuration.DiagnosticConfig(
                      debug_config = debug_config)

with diagnostic.diagnose(diagnostic_config):
	main_method()  # this is the main method to run
```
This configuration will start collecting stack traces inside the `/tmp/debugging` directory on each Kubernetes Pod.

### Explore Stack Traces
To explore the stack traces collected in a temporary directory in Kubernetes Pod, you can run the following command to configure a sidecar container that will read the traces from `/tmp/debugging` directory.
 ```shell
 python3 xpk.py workload create \
  --workload xpk-test-workload --command "python3 main.py" --cluster \
  xpk-test --tpu-type=v5litepod-16 --deploy-stacktrace-sidecar
 ```

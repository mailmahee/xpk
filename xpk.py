"""
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
"""

r"""xpk (Accelerated Processing Kit).

Next Steps:
- Cluster describe is broken by Cacheimage since that counts as a workload.
- Cluster describe: count by jobset.
- If any instance goes down, bring down the whole job.
- How to more gracefully handle job failures, distinguishing between software
  and infra?
- Look into --docker-name and --docker-image.
  Shouldn't one string be adequate to express what we want?
- Apply learnings from about private, region, coredns, etc:
- Enable special preheater
- Make Argparse logic this a function?
  - Obvious logic that starts in main instead of here in code but args will
    not be a universal argument.
"""

import argparse
import datetime
import os
import random
import re
import string
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

################### Compatibility Check ###################
# Check that the user runs the below version or greater.

major_version_supported = 3
minor_version_supported = 10

user_major_version = sys.version_info[0]
user_minor_version = sys.version_info[1]
if (
    user_major_version < major_version_supported
    or user_minor_version < minor_version_supported
):
  raise RuntimeError('xpk must be run with Python'
      f' {major_version_supported}.{minor_version_supported} or greater.'
      f' User currently is running {user_major_version}.{user_minor_version}'
  )


################### Internally used constants ##############

default_docker_image = 'python:3.10'
default_script_dir = os.getcwd()
default_gke_version="1.28.3-gke.1286000"

workload_create_yaml = """apiVersion: jobset.x-k8s.io/v1alpha2
kind: JobSet
metadata:
  name: {args.workload}
  labels:
    kueue.x-k8s.io/queue-name: multislice-queue  # Name of the LocalQueue
    xpk.google.com/workload: {args.workload}
  annotations:
    alpha.jobset.sigs.k8s.io/exclusive-topology: cloud.google.com/gke-nodepool # 1:1 job replica to node pool assignment
spec:
  failurePolicy:
    maxRestarts: {args.max_restarts}
  replicatedJobs:
    - name: slice-job
      replicas: {args.num_slices}
      template:
        spec:
          parallelism: {system.vms_per_slice}    # Equal to the number of VMs per slice
          completions: {system.vms_per_slice}    # Same as the above.
          backoffLimit: 0   # When any pod fails, the job is failed
          template:
            metadata:
              labels:
                xpk.google.com/workload: {args.workload}
            spec:
              schedulerName: {args.scheduler}
              restartPolicy: Never
              {affinity}
              nodeSelector:
                {accelerator_label}
                {machine_label}
              priorityClassName: {args.priority}
              hostNetwork: true
              dnsPolicy: ClusterFirstWithHostNet
              terminationGracePeriodSeconds: {args.termination_grace_period_seconds}
              containers:
              {container}
                {env}
                volumeMounts:
                - mountPath: /dev/shm
                  name: dshm-2
              volumes:
              - emptyDir:
                  medium: Memory
                name: dshm-2
"""

workload_delete_yaml = """apiVersion: jobset.x-k8s.io/v1alpha2
kind: JobSet
metadata:
  name: {args.workload}
  annotations:
    alpha.jobset.sigs.k8s.io/exclusive-topology: cloud.google.com/gke-nodepool # 1:1 job replica to node pool assignment
"""

script_dir_dockerfile = """FROM {base_docker_image}

# Set the working directory in the container
WORKDIR /app

# Copy all files from local workspace into docker container
COPY . .

WORKDIR /app
"""

cluster_set_crd_yaml = """apiVersion: kueue.x-k8s.io/v1beta1
kind: ResourceFlavor
metadata:
  name: {cluster_hardware_name}
spec:
  nodeLabels:
    {accelerator_label}
    {machine_label}
---
apiVersion: kueue.x-k8s.io/v1beta1
kind: ClusterQueue
metadata:
  name: "cluster-queue"
spec:
  preemption:
      reclaimWithinCohort: Never # Don't preempt other queues in the cohort.
      withinClusterQueue: LowerPriority
  namespaceSelector: {{}} # match all.
  resourceGroups:
  - coveredResources: ["{resource_type}"]
    flavors:
    - name: {cluster_hardware_name}
      resources:
      - name: "{resource_type}"
        nominalQuota: {total_chips}  # Number of slices * number of chips in each slice
---
apiVersion: kueue.x-k8s.io/v1beta1
kind: LocalQueue
metadata:
  namespace: default
  name: multislice-queue
spec:
  clusterQueue: cluster-queue
---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: very-low
value: 100
globalDefault: false
description: "Very Low"
---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: low
value: 250
globalDefault: false
description: "Low"
---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: medium
value: 500
globalDefault: false
description: "Medium"
---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: high
value: 750
globalDefault: false
description: "High"
---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: very-high
value: 1000
globalDefault: false
description: "Very High"
"""

cluster_preheat_yml = """
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: {cachekey}
  labels:
    k8s-app: {cachekey}
spec:
  selector:
    matchLabels:
      k8s-app: {cachekey}
  updateStrategy:
    type: RollingUpdate
  template:
    metadata:
      labels:
        name: {cachekey}
        k8s-app: {cachekey}
    spec:
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
            - matchExpressions:
              - key: {nodeSelectorKey}
                operator: Exists
      tolerations:
      - operator: "Exists"
      containers:
      - image: {image_name}
        name: {cachekey}
        command: [ "sleep", "inf" ]
"""

cluster_configmap_yaml = """kind: ConfigMap
apiVersion: v1
metadata:
  name: {args.cluster}-resources-configmap
data:
  {data}
"""

AcceleratorType = {
  'TPU': 1,
  'GPU': 2,
  'CPU': 3
}

@dataclass
class AcceleratorCharacteristics:
  resource_type: str
  accelerator_label: int
  machine_label: str

AcceleratorTypeToAcceleratorCharacteristics = {
     # TPU
      AcceleratorType['TPU']: AcceleratorCharacteristics(
      'google.com/tpu', 'cloud.google.com/gke-tpu-accelerator', 'cloud.google.com/gke-tpu-topology'
      ),
     # GPU
      AcceleratorType['GPU']: AcceleratorCharacteristics(
      'nvidia.com/gpu', 'cloud.google.com/gke-accelerator', 'cloud.google.com/gce-machine-type'
      ),
     # CPU
      AcceleratorType['CPU']: AcceleratorCharacteristics(
      'cpu', '', 'cloud.google.com/gke-nodepool' 
      )
}


@dataclass
class SystemCharacteristics:
  topology: str
  vms_per_slice: int
  gke_accelerator: str
  gce_machine_type: str
  chips_per_vm: int
  accelerator_type: AcceleratorType
  device_type: str

################### Subcommand Helper Functions #############
""" !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
IF YOU MODIFY THE BELOW UserFacingNameToSystemCharacteristics MAP YOU SHOULD ALSO ADD CORRESPONDING
MODIFICATIONS TO UserFacingNameToSystemCharacteristics IN MaxText/accelerator_to_spec_map.py !!!!! """
# vvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvv
UserFacingNameToSystemCharacteristics = {
    # GPU system charcteristics
    # A100-40gb-$CHIPS
    'a100-40gb-1': SystemCharacteristics(
      'N/A', 1, 'nvidia-tesla-a100', 'a2-highgpu-1g', 1, AcceleratorType['GPU'], 'a100-40gb-1'
    ),
    'a100-40gb-2': SystemCharacteristics(
      'N/A', 1, 'nvidia-tesla-a100', 'a2-highgpu-2g', 2, AcceleratorType['GPU'], 'a100-40gb-2'
    ),
    'a100-40gb-4': SystemCharacteristics(
      'N/A', 1, 'nvidia-tesla-a100', 'a2-highgpu-4g', 4, AcceleratorType['GPU'], 'a100-40gb-4'
    ),
    'a100-40gb-8': SystemCharacteristics(
      'N/A', 1, 'nvidia-tesla-a100', 'a2-highgpu-8g', 8, AcceleratorType['GPU'], 'a100-40gb-8'
    ),
    # H100-80gb-$CHIPS
    'h100-80gb-8': SystemCharacteristics(
      'N/A', 1, 'nvidia-h100-80gb', 'a3-highgpu-8g', 8, AcceleratorType['GPU'], 'h100-80gb-8'
    ),

    # TPU system charcteristics
    # v5p
    'v5p-8': SystemCharacteristics(
      '2x2x1', 1, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-8'
    ),
    'v5p-16': SystemCharacteristics(
      '2x2x2', 2, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-16'
    ),
    'v5p-32': SystemCharacteristics(
      '2x2x4', 4, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-32'
    ),
    'v5p-64': SystemCharacteristics(
      '2x4x4', 8, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-64'
    ),
    'v5p-128': SystemCharacteristics(
      '4x4x4', 16, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-128'
    ),
    'v5p-256': SystemCharacteristics(
      '4x4x8', 32, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-256'
    ),
    'v5p-384': SystemCharacteristics(
      '4x4x12', 48, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-384'
    ),
    'v5p-512': SystemCharacteristics(
      '4x8x8', 64, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-512'
    ),
    'v5p-640': SystemCharacteristics(
      '4x4x20', 80, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-640'
    ),
    'v5p-768': SystemCharacteristics(
      '4x8x12', 96, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-768'
    ),
    'v5p-896': SystemCharacteristics(
      '4x4x28', 112, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-896'
    ),
    'v5p-1024': SystemCharacteristics(
      '8x8x8', 128, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-1024'
    ),
    'v5p-1152': SystemCharacteristics(
      '4x12x12', 144, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-1152'
    ),
    'v5p-1280': SystemCharacteristics(
      '4x8x20', 160, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-1280'
    ),
    'v5p-1408': SystemCharacteristics(
      '4x4x44', 176, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-1408'
    ),
    'v5p-1536': SystemCharacteristics(
      '8x8x12', 192, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-1536'
    ),
    'v5p-1664': SystemCharacteristics(
      '4x4x52', 208, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-1664'
    ),
    'v5p-1792': SystemCharacteristics(
      '4x8x28', 224, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-1792'
    ),
    'v5p-1920': SystemCharacteristics(
      '4x12x20', 240, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-1920'
    ),
    'v5p-2048': SystemCharacteristics(
      '8x8x16', 256, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-2048'
    ),
    'v5p-2176': SystemCharacteristics(
      '4x4x68', 272, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-2176'
    ),
    'v5p-2304': SystemCharacteristics(
      '8x12x12', 288, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-2304'
    ),
    'v5p-2432': SystemCharacteristics(
      '4x4x76', 304, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-2432'
    ),
    'v5p-2560': SystemCharacteristics(
      '8x8x20', 320, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-2560'
    ),
    'v5p-2688': SystemCharacteristics(
      '4x12x28', 336, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-2688'
    ),
    'v5p-2816': SystemCharacteristics(
      '4x8x44', 352, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-2816'
    ),
    'v5p-2944': SystemCharacteristics(
      '4x4x92', 368, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-2944'
    ),
    'v5p-3072': SystemCharacteristics(
      '4x12x16', 384, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-3072'
    ),
    'v5p-3200': SystemCharacteristics(
      '4x20x20', 400, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-3200'
    ),
    'v5p-3328': SystemCharacteristics(
      '4x8x52', 416, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-3328'
    ),
    'v5p-3456': SystemCharacteristics(
      '12x12x12', 432, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-3456'
    ),
    'v5p-3584': SystemCharacteristics(
      '8x8x28', 448, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-3584'
    ),
    'v5p-3712': SystemCharacteristics(
      '4x4x116', 464, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-3712'
    ),
    'v5p-3840': SystemCharacteristics(
      '8x12x20', 480, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-3840'
    ),
    'v5p-3968': SystemCharacteristics(
      '4x4x124', 496, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-3968'
    ),
    'v5p-4096': SystemCharacteristics(
      '8x16x16', 512, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-4096'
    ),
    'v5p-4224': SystemCharacteristics(
      '4x12x44', 528, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-4224'
    ),
    'v5p-4352': SystemCharacteristics(
      '4x8x68', 544, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-4352'
    ),
    'v5p-4480': SystemCharacteristics(
      '4x20x28', 560, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-4480'
    ),
    'v5p-4608': SystemCharacteristics(
      '12x12x16', 576, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-4608'
    ),
    'v5p-4736': SystemCharacteristics(
      '4x4x148', 592, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-4736'
    ),
    'v5p-4864': SystemCharacteristics(
      '4x8x76', 608, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-4864'
    ),
    'v5p-4992': SystemCharacteristics(
      '4x12x52', 624, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-4992'
    ),
    'v5p-5120': SystemCharacteristics(
      '8x16x20', 640, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-5120'
    ),
    'v5p-5248': SystemCharacteristics(
      '4x4x164', 656, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-5248'
    ),
    'v5p-5376': SystemCharacteristics(
      '8x12x28', 672, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-5376'
    ),
    'v5p-5504': SystemCharacteristics(
      '4x4x172', 688, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-5504'
    ),
    'v5p-5632': SystemCharacteristics(
      '8x8x44', 704, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-5632'
    ),
    'v5p-5760': SystemCharacteristics(
      '12x12x20', 720, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-5760'
    ),
    'v5p-5888': SystemCharacteristics(
      '4x8x92', 736, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-5888'
    ),
    'v5p-6016': SystemCharacteristics(
      '4x4x188', 752, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-6016'
    ),
    'v5p-6144': SystemCharacteristics(
      '12x16x16', 768, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-6144'
    ),
    'v5p-6272': SystemCharacteristics(
      '4x28x28', 784, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-6272'
    ),
    'v5p-6400': SystemCharacteristics(
      '8x20x20', 800, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-6400'
    ),
    'v5p-6528': SystemCharacteristics(
      '4x12x68', 816, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-6528'
    ),
    'v5p-6656': SystemCharacteristics(
      '8x8x52', 832, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-6656'
    ),
    'v5p-6784': SystemCharacteristics(
      '4x4x212', 848, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-6784'
    ),
    'v5p-6912': SystemCharacteristics(
      '12x12x24', 864, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-6912'
    ),
    'v5p-7040': SystemCharacteristics(
      '4x20x44', 880, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-7040'
    ),
    'v5p-7168': SystemCharacteristics(
      '8x16x28', 896, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-7168'
    ),
    'v5p-7296': SystemCharacteristics(
      '4x12x76', 912, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-7296'
    ),
    'v5p-7424': SystemCharacteristics(
      '4x8x116', 928, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-7424'
    ),
    'v5p-7552': SystemCharacteristics(
      '4x4x236', 944, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-7552'
    ),
    'v5p-7680': SystemCharacteristics(
      '12x16x20', 960, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-7680'
    ),
    'v5p-7808': SystemCharacteristics(
      '4x4x244', 976, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-7808'
    ),
    'v5p-7936': SystemCharacteristics(
      '4x8x124', 992, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-7936'
    ),
    'v5p-8064': SystemCharacteristics(
      '12x12x28', 1008, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-8064'
    ),
    'v5p-8192': SystemCharacteristics(
      '16x16x16', 1024, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-8192'
    ),
    'v5p-8320': SystemCharacteristics(
      '4x20x52', 1040, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-8320'
    ),
    'v5p-8448': SystemCharacteristics(
      '8x12x44', 1056, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-8448'
    ),
    'v5p-8704': SystemCharacteristics(
      '8x8x68', 1088, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-8704'
    ),
    'v5p-8832': SystemCharacteristics(
      '4x12x92', 1104, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-8832'
    ),
    'v5p-8960': SystemCharacteristics(
      '8x20x28', 1120, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-8960'
    ),
    'v5p-9216': SystemCharacteristics(
      '12x16x24', 1152, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-9216'
    ),
    'v5p-9472': SystemCharacteristics(
      '4x8x148', 1184, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-9472'
    ),
    'v5p-9600': SystemCharacteristics(
      '12x20x20', 1200, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-9600'
    ),
    'v5p-9728': SystemCharacteristics(
      '8x8x76', 1216, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-9728'
    ),
    'v5p-9856': SystemCharacteristics(
      '4x28x44', 1232, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-9856'
    ),
    'v5p-9984': SystemCharacteristics(
      '8x12x52', 1248, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-9984'
    ),
    'v5p-10240': SystemCharacteristics(
      '16x16x20', 1280, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-10240'
    ),
    'v5p-10368': SystemCharacteristics(
      '12x12x36', 1296, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-10368'
    ),
    'v5p-10496': SystemCharacteristics(
      '4x8x164', 1312, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-10496'
    ),
    'v5p-10752': SystemCharacteristics(
      '12x16x28', 1344, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-10752'
    ),
    'v5p-10880': SystemCharacteristics(
      '4x20x68', 1360, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-10880'
    ),
    'v5p-11008': SystemCharacteristics(
      '4x8x172', 1376, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-11008'
    ),
    'v5p-11136': SystemCharacteristics(
      '4x12x116', 1392, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-11136'
    ),
    'v5p-11264': SystemCharacteristics(
      '8x16x44', 1408, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-11264'
    ),
    'v5p-11520': SystemCharacteristics(
      '12x20x24', 1440, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-11520'
    ),
    'v5p-11648': SystemCharacteristics(
      '4x28x52', 1456, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-11648'
    ),
    'v5p-11776': SystemCharacteristics(
      '8x8x92', 1472, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-11776'
    ),
    'v5p-11904': SystemCharacteristics(
      '4x12x124', 1488, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-11904'
    ),
    'v5p-12032': SystemCharacteristics(
      '4x8x188', 1504, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-12032'
    ),
    'v5p-12160': SystemCharacteristics(
      '4x20x76', 1520, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-12160'
    ),
    'v5p-12288': SystemCharacteristics(
      '16x16x24', 1536, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-12288'
    ),
    'v5p-13824': SystemCharacteristics(
      '12x24x24', 1728, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-13824'
    ),
    'v5p-17920': SystemCharacteristics(
      '16x20x28', 2240, 'tpu-v5p-slice', 'ct5p-hightpu-4t', 4, AcceleratorType['TPU'], 'v5p-17920'
    ),
    # v5litepod
    'v5litepod-16': SystemCharacteristics(
        '4x4', 4, 'tpu-v5-lite-podslice', 'ct5lp-hightpu-4t', 4, AcceleratorType['TPU'], 'v5litepod-16'
    ),
    'v5litepod-32': SystemCharacteristics(
        '4x8', 8, 'tpu-v5-lite-podslice', 'ct5lp-hightpu-4t', 4, AcceleratorType['TPU'], 'v5litepod-32'
    ),
    'v5litepod-64': SystemCharacteristics(
        '8x8', 16, 'tpu-v5-lite-podslice', 'ct5lp-hightpu-4t', 4, AcceleratorType['TPU'], 'v5litepod-64'
    ),
    'v5litepod-128': SystemCharacteristics(
        '8x16', 32, 'tpu-v5-lite-podslice', 'ct5lp-hightpu-4t', 4, AcceleratorType['TPU'], 'v5litepod-128'
    ),
    'v5litepod-256': SystemCharacteristics(
        '16x16', 64, 'tpu-v5-lite-podslice', 'ct5lp-hightpu-4t', 4, AcceleratorType['TPU'], 'v5litepod-256'
    ),
    # v4
    'v4-8': SystemCharacteristics(
      '2x2x1', 1,'tpu-v4-podslice', 'ct4p-hightpu-4t', 4, AcceleratorType['TPU'], 'v4-8'
    ),
    'v4-16': SystemCharacteristics(
      '2x2x2', 2,'tpu-v4-podslice', 'ct4p-hightpu-4t', 4, AcceleratorType['TPU'], 'v4-16'
    ),
    'v4-32': SystemCharacteristics(
      '2x2x4', 4,'tpu-v4-podslice', 'ct4p-hightpu-4t', 4, AcceleratorType['TPU'], 'v4-32'
    ),
    'v4-64': SystemCharacteristics(
      '2x4x4', 8,'tpu-v4-podslice', 'ct4p-hightpu-4t', 4, AcceleratorType['TPU'], 'v4-64'
    ),
    'v4-128': SystemCharacteristics(
      '4x4x4', 16,'tpu-v4-podslice', 'ct4p-hightpu-4t', 4, AcceleratorType['TPU'], 'v4-128'
    ),
    'v4-256': SystemCharacteristics(
      '4x4x8', 32,'tpu-v4-podslice', 'ct4p-hightpu-4t', 4, AcceleratorType['TPU'], 'v4-256'
    ),
    'v4-512': SystemCharacteristics(
      '4x8x8', 64,'tpu-v4-podslice', 'ct4p-hightpu-4t', 4, AcceleratorType['TPU'], 'v4-512'
    ),
    'v4-1024': SystemCharacteristics(
      '8x8x8', 128,'tpu-v4-podslice', 'ct4p-hightpu-4t', 4, AcceleratorType['TPU'], 'v4-1024'
    ),
    'v4-1536': SystemCharacteristics(
      '8x8x12', 192,'tpu-v4-podslice', 'ct4p-hightpu-4t', 4, AcceleratorType['TPU'], 'v4-1536'
    ),
    'v4-2048': SystemCharacteristics(
      '8x8x16', 256,'tpu-v4-podslice', 'ct4p-hightpu-4t', 4, AcceleratorType['TPU'], 'v4-2048'
    ),
    'v4-4096': SystemCharacteristics(
      '8x16x16', 512,'tpu-v4-podslice', 'ct4p-hightpu-4t', 4, AcceleratorType['TPU'], 'v4-4096'
    ),

    # CPU system characteristics
    # n2-standard-32-$VMs
    'n2-standard-32-1': SystemCharacteristics(
      'N/A', 1,'N/A', 'n2-standard-32', 1, AcceleratorType['CPU'], 'n2-standard-32-1'
    ),
    'n2-standard-32-2': SystemCharacteristics(
      'N/A', 2,'N/A', 'n2-standard-32', 1, AcceleratorType['CPU'], 'n2-standard-32-2'
    ),
    'n2-standard-32-4': SystemCharacteristics(
      'N/A', 4,'N/A', 'n2-standard-32', 1, AcceleratorType['CPU'], 'n2-standard-32-4'
    ),
    'n2-standard-32-8': SystemCharacteristics(
      'N/A', 8,'N/A', 'n2-standard-32', 1, AcceleratorType['CPU'], 'n2-standard-32-8'
    ),
    'n2-standard-32-16': SystemCharacteristics(
      'N/A', 16,'N/A', 'n2-standard-32', 1, AcceleratorType['CPU'], 'n2-standard-32-16'
    ),
    'n2-standard-32-32': SystemCharacteristics(
      'N/A', 32,'N/A', 'n2-standard-32', 1, AcceleratorType['CPU'], 'n2-standard-32-32'
    ),
    'n2-standard-32-64': SystemCharacteristics(
      'N/A', 64,'N/A', 'n2-standard-32', 1, AcceleratorType['CPU'], 'n2-standard-32-64'
    ),
    'n2-standard-32-128': SystemCharacteristics(
      'N/A', 128,'N/A', 'n2-standard-32', 1, AcceleratorType['CPU'], 'n2-standard-32-128'
    ),
    'n2-standard-32-256': SystemCharacteristics(
      'N/A', 256,'N/A', 'n2-standard-32', 1, AcceleratorType['CPU'], 'n2-standard-32-256'
    ),
    'n2-standard-32-512': SystemCharacteristics(
      'N/A', 512,'N/A', 'n2-standard-32', 1, AcceleratorType['CPU'], 'n2-standard-32-512'
    ),
    'n2-standard-32-1024': SystemCharacteristics(
      'N/A', 1024,'N/A', 'n2-standard-32', 1, AcceleratorType['CPU'], 'n2-standard-32-1024'
    ),
    'n2-standard-32-2048': SystemCharacteristics(
      'N/A', 2048,'N/A', 'n2-standard-32', 1, AcceleratorType['CPU'], 'n2-standard-32-2048'
    ),
}
""" If you modify UserFacingNameToSystemCharacteristics you should also modify the corresponding
Map in MaxText/accelerator_to_spec_map.py """
# ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

def chunks(lst, n):
  """Return a list of n-sized chunks from lst.

  Args:
    lst: input list to get chunks from.
    n: size of each chunk.

  Returns:
    List of n-sized chunks for lst.
  """
  return [lst[i:i+n] for i in range(0, len(lst), n)]


def make_tmp_files(per_command_name):
  """Make temporary files for each command.

  Args:
    per_command_name: list of command names.

  Returns:
    A list of temporary files for each command.
  """
  return [
      tempfile.NamedTemporaryFile(delete=False, prefix=command + '-')
      for command in per_command_name
  ]


def run_commands(commands, jobname, per_command_name, batch=10, dry_run=False):
  """Run commands in groups of `batch`.

  Args:
    commands: list of command.
    jobname: the name of the job.
    per_command_name: list of command names.
    batch: number of commands to run in parallel.
    dry_run: enables dry_run if set to true.

  Returns:
    0 if successful and 1 otherwise.
  """
  temporary_files_batches = chunks(make_tmp_files(per_command_name), batch)
  commands_batched = chunks(commands, batch)
  per_command_name_batches = chunks(per_command_name, batch)

  xpk_print(
      f'Breaking up a total of {len(commands)} commands into'
      f' {len(commands_batched)} batches'
  )
  if dry_run:
    xpk_print('Pretending all the jobs succeeded')
    return 0

  max_return_code = 0
  for i, _ in enumerate(commands_batched):
    xpk_print(f'Dispatching batch {i}/{len(commands_batched)}')
    batch_max_return_code, _ = run_command_batch(
        commands_batched[i],
        jobname,
        per_command_name_batches[i],
        temporary_files_batches[i],
    )
    max_return_code = max(max_return_code, batch_max_return_code)
    if max_return_code > 0:
      return max_return_code
  return max_return_code


def run_command_batch(commands, jobname, per_command_name, output_logs):
  """Runs commands in parallel.

  Args:
    commands: list of n commands, each command is a a list of strings
    jobname: Useful debugging name for the group of commands
    per_command_name: specific name per task
    output_logs: list of n log paths, each command will output to each log.

  Returns:
    The max return code and a list of all the return codes.
  """

  children = []
  start_time = datetime.datetime.now()
  for i, command in enumerate(commands):
    children.append(
        # subprocess managed by list pylint: disable=consider-using-with
        subprocess.Popen(
            command, stdout=output_logs[i], stderr=output_logs[i], shell=True
        )
    )

  while True:
    returncodes = [child.poll() for child in children]
    max_returncode = max([0] + [r for r in returncodes if r is not None])
    completed = len([r for r in returncodes if r is not None])
    total = len(returncodes)
    seconds_elapsed = (datetime.datetime.now() - start_time).total_seconds()
    if completed < total:
      slow_worker_index = returncodes.index(None)
      slow_worker_text = per_command_name[slow_worker_index]
      slow_str = (
          f', task {slow_worker_text} still working, logfile'
          f' {output_logs[slow_worker_index].name}'
      )
    else:
      slow_str = ''
    xpk_print(
        f'[t={seconds_elapsed:.2f}, {jobname}] Completed'
        f' {completed}/{total}{slow_str}'
    )
    if max_returncode > 0:
      failing_index = [
          i for i, x in enumerate(returncodes) if x is not None and x > 0
      ][0]
      xpk_print(
          f'Terminating all {jobname} processes since at least one failed.'
      )
      xpk_print(
          f'Failure is {per_command_name[failing_index]}'
          f' and logfile {output_logs[failing_index].name}'
      )
      for child in children:
        child.terminate()
      break

    if completed == total:
      break

    time.sleep(1)
  return max_returncode, returncodes


def add_zone_and_project(args):
  """Obtains the zone and project names from gcloud configs if not defined.

  Args:
    args: user provided arguments for running the command.
  """
  if not args.project:
    args.project = get_project()
  if not args.zone:
    args.zone = get_zone()
  xpk_print(f'Working on {args.project=} and {args.zone}')


def add_env_config(args):
  """Adds environment configurations to the jobset config.

  Args:
    args: user provided arguments for running the command.
  """
  env = {'JOBSET_NAME': args.workload}
  if args.env_file:
    print('Setting container environment from', args.env_file)
    pat = re.compile(r'(^[a-zA-Z_][a-zA-Z0-9_]*?)(?:=(.*))$', re.M)
    with open(file=args.env_file, mode='r', encoding='utf-8') as f:
      for match in pat.finditer(f.read()):
        variable = match.group(1)
        if len(match.groups()) > 1:
          env[variable] = match.group(2)
        else:
          assert variable in os.environ, (
              f'Variable {variable} is not set in the current '
              'environment, a value must be specified.'
          )
          env[variable] = os.environ[variable]

  if args.debug_dump_gcs:
    if 'XLA_FLAGS' in env:
      raise ValueError('Conflict: XLA_FLAGS defined in both --debug_dump_gcs '
                       'and environment file. Please choose one way to define '
                       'XLA_FLAGS.')
    env['XLA_FLAGS'] = '--xla_dump_to=/tmp/xla_dump/'

  env_format = '''
                - name: {key}
                  value: "{value}"'''
  args.env = ''.join(env_format.format(key=k, value=v) for k, v in env.items())


def write_temporary_file(payload):
  """Writes `payload` to a temporary file.

  Args:
    payload: The string to be written to the file.

  Returns:
    A file object that was written to.
  """
  with tempfile.NamedTemporaryFile(delete=False) as tmp:
    with open(file=tmp.name, mode='w', encoding='utf=8') as f:
      f.write(payload)
      f.flush()
    return tmp


def run_command_for_value(
    command, task, global_args, dry_run_return_val='0'
) -> tuple[int, str]:
  """Runs the command and returns the error code and stdout.

  Prints errors and associated user-facing information

  Args:
    command: user provided command to run.
    task: user provided task name for running the command.
    global_args: user provided arguments for running the command.
    dry_run_return_val: return value of this command for dry run.

  Returns:
    tuple[int, str]
    int: return_code, default is 0
    str: return_val, default is '0'
  """
  if global_args.dry_run:
    xpk_print(
        f'Task: `{task}` is implemented by the following command'
        ' not running since it is a dry run.'
        f' \n{command}'
    )
    return 0, dry_run_return_val
  else:
    xpk_print(
        f'Task: `{task}` is implemented by `{command}`, hiding output unless'
        ' there is an error.'
    )
    try:
      output = subprocess.check_output(
          command,
          shell=True,
          stderr=subprocess.STDOUT,
      )
    except subprocess.CalledProcessError as e:
      xpk_print(f'Task {task} failed with {e.returncode}')
      xpk_print('*' * 80)
      xpk_print(e.output)
      xpk_print('*' * 80)
      return e.returncode, str(e.output, 'UTF-8')
    return 0, str(output, 'UTF-8')


def run_command_with_updates(command, task, global_args, verbose=True) -> int:
  """Generic run commands function with updates.

  Args:
    command: command to execute
    task: user-facing name of the task
    global_args: user provided arguments for running the command.
    verbose: shows stdout and stderr if set to true. Set to True by default.

  Returns:
    0 if successful and 1 otherwise.
  """
  if global_args.dry_run:
    xpk_print(
        f'Task: `{task}` is implemented by the following command'
        ' not running since it is a dry run.'
        f' \n{command}'
    )
    return 0
  if verbose:
    xpk_print(
        f'Task: `{task}` is implemented by `{command}`, streaming output live.'
    )
    with subprocess.Popen(
        command,
        stdout=sys.stdout,
        stderr=sys.stderr,
        shell=True,
    ) as child:
      i = 0
      while True:
        return_code = child.poll()
        if return_code is None:
          xpk_print(f'Waiting for `{task}`, for {i} seconds')
          time.sleep(1)
          i += 1
        else:
          xpk_print(f'Task: `{task}` terminated with code `{return_code}`')
          return return_code
  else:
    xpk_print(
        f'Task: `{task}` is implemented by `{command}`, hiding output unless'
        ' there is an error.'
    )
    try:
      subprocess.check_output(command, shell=True, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
      xpk_print(
          f'Task: `{task}` terminated with ERROR `{e.returncode}`, printing'
          ' logs'
      )
      xpk_print('*' * 80)
      xpk_print(e.output)
      xpk_print('*' * 80)
      return e.returncode
    xpk_print(f'Task: `{task}` succeeded.')
    return 0


def xpk_print(*args, **kwargs):
  """Helper function to print a prefix before function provided args.

  Args:
    *args: user provided print args.
    **kwargs: user provided print args.
  """
  sys.stdout.write('[XPK] ')
  print(*args, **kwargs)
  sys.stdout.flush()


def xpk_exit(error_code):
  """Helper function to exit xpk with an associated error code.

  Args:
    error_code: If the code provided is zero, then no issues occurred.
  """
  if error_code == 0:
    xpk_print('Exiting XPK cleanly')
    sys.exit(0)
  else:
    xpk_print(f'XPK failed, error code {error_code}')
    sys.exit(error_code)


def get_project():
  """Get GCE project from `gcloud config get project`.

  Returns:
     The project name.
  """
  completed_command = subprocess.run(
      ['gcloud', 'config', 'get', 'project'], check=True, capture_output=True
  )
  project_outputs = completed_command.stdout.decode().strip().split('\n')
  if len(project_outputs) < 1 or project_outputs[-1] == '':
    sys.exit(
        'You must specify the project in the project flag or set it with'
        " 'gcloud config set project <project>'"
    )
  return project_outputs[
      -1
  ]  # The project name lives on the last line of the output


def get_zone():
  """Get GCE zone from `gcloud config get compute/zone`.

  Returns:
     The zone name.
  """
  completed_command = subprocess.run(
      ['gcloud', 'config', 'get', 'compute/zone'],
      check=True,
      capture_output=True,
  )
  zone_outputs = completed_command.stdout.decode().strip().split('\n')
  if len(zone_outputs) < 1 or zone_outputs[-1] == '':
    sys.exit(
        "You must specify the zone in the zone flag or set it with 'gcloud"
        " config set compute/zone <zone>'"
    )
  return zone_outputs[-1]  # The zone name lives on the last line of the output


def zone_to_region(zone) -> str:
  """Helper function converts zone name to region name.

  Args:
    zone: zone name.

  Returns:
     The region name.
  """
  zone_terms = zone.split('-')
  return zone_terms[0] + '-' + zone_terms[1]


def run_gke_cluster_create_command(args) -> int:
  """Run the Create GKE Cluster request.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  # cluster_cpu_machine_type is soon to be deprecated!
  machine_type = args.default_pool_cpu_machine_type
  if args.cluster_cpu_machine_type != '':
    xpk_print('Warning: Note that cluster-cpu-machine-type is soon to be',
              ' deprecated. Please use --default-pool-cpu-machine-type instead,'
              ' to denote the machine type of the default cpu node pool. Set'
              ' the machine type of other cpu nodepools using `--device-type`.')
    machine_type = args.cluster_cpu_machine_type

  # Create the regional cluster with `num-nodes` CPU nodes in the same zone as
  # TPUs. This has been tested with clusters of 300 VMs. Larger clusters will
  # benefit from a larger initial `--num-nodes`. After the cluster is created,
  # the auto-scaler can reduce/increase the nodes based on the load.
  command = (
      'gcloud beta container clusters create'
      f' {args.cluster} --release-channel rapid --enable-autoscaling'
      ' --total-min-nodes 1 --total-max-nodes 1000 --num-nodes 6'
      f' --node-locations={args.zone}'
      f' --project={args.project} --region={zone_to_region(args.zone)}'
      f' --cluster-version={args.gke_version} --location-policy=BALANCED'
      f' --machine-type={machine_type}'
      ' --scopes=storage-full,gke-default'
      f' {args.custom_cluster_arguments}'
  )
  return_code = run_command_with_updates(command, 'GKE Cluster Create', args)
  if return_code != 0:
    xpk_print(f'GKE Cluster Create request returned ERROR {return_code}')
    return 1

  return 0


def create_cluster_configmap(args, system):
  """Run the Create GKE Cluster ConfigMap request.

  Args:
    args: user provided arguments for running the command.
    system: system characteristics.

  Returns:
    0 if successful and 1 otherwise.
  """
  device_type = args.tpu_type if args.tpu_type else args.device_type
  data = f'{device_type}: "{int(args.num_slices) * system.vms_per_slice}"'
  yml_string = cluster_configmap_yaml.format(args=args,
                                           data=data)
  tmp = write_temporary_file(yml_string)
  command = f'kubectl apply -f {str(tmp.file.name)}'

  return_code = run_command_with_updates(command, 'GKE Cluster Create ConfigMap', args)
  if return_code != 0:
    xpk_print(f'GKE Cluster Create ConfigMap request returned ERROR {return_code}')
    return 1

  return 0


def get_cluster_configmap(args):
  """Run the Get GKE Cluster ConfigMap request.

  Args:
    args: user provided arguments for running the command.

  Returns:
    key:value pairs stored in cluster ConfigMap.
  """
  command = (
    f'kubectl get configmap {args.cluster}-resources-configmap -o=custom-columns="ConfigData:data" --no-headers=true'
  )

  return_code, return_value = run_command_for_value(command, 'GKE Cluster Get ConfigMap', args)
  if return_code != 0:
    xpk_print(f'GKE Cluster Get ConfigMap request returned ERROR {return_code}')
    return None

  config_map = {}
  return_value = return_value.strip()
  if return_value:
    # Format of ConfigMap: map[key1:value1 key2:value2]
    configs = return_value[4:-1].split(" ")
    for config in configs:
      key, value = config.strip().split(":")
      config_map[key] = int(value)
  return config_map


def get_all_clusters_programmatic(args) -> tuple[list[str], int]:
  """Gets all the clusters associated with the project / region.

  Args:
    args: user provided arguments for running the command.

  Returns:
    List of cluster names and 0 if successful and 1 otherwise.
  """
  command = (
      'gcloud container clusters list'
      f' --project={args.project} --region={zone_to_region(args.zone)}'
  )
  return_code, raw_cluster_output = run_command_for_value(
      command, 'Find if Cluster Exists', args
  )
  if return_code != 0:
    xpk_print(f'Find if Cluster Exists returned ERROR {return_code}')
    return [], return_code
  cluster_names = [x.split(' ')[0] for x in raw_cluster_output.splitlines()]
  return cluster_names, 0


def create_cluster_if_necessary(args) -> int:
  all_clusters, return_code = get_all_clusters_programmatic(args)
  if return_code > 0:
    xpk_print('Listing all clusters failed!')
    return 1
  if args.cluster in all_clusters:
    xpk_print('Skipping cluster creation since it already exists')
    return 0
  else:
    return run_gke_cluster_create_command(args)


def get_all_nodepools_programmatic(args) -> tuple[list[str], int]:
  """Gets all the nodepools associated with the cluster / project / region.

  Args:
    args: user provided arguments for running the command.

  Returns:
    List of nodepools and 0 if successful and 1 otherwise.
  """
  command = (
      'gcloud beta container node-pools list'
      ' --cluster'
      f' {args.cluster} --project={args.project} --region={zone_to_region(args.zone)}'
  )
  return_code, raw_nodepool_output = (
      run_command_for_value(command, 'Get All Node Pools', args)
  )
  if return_code != 0:
    xpk_print(f'Get All Node Pools returned ERROR {return_code}')
    return [], 1

  all_nodepools = [x.split(' ')[0] for x in raw_nodepool_output.splitlines()]
  return all_nodepools, 0


def print_reservations(args) -> int:
  """Print the reservations in the project.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  command = (
        f'gcloud beta compute reservations list --project={args.project}'
    )
  return_code = (
      run_command_with_updates(
          command, 'Get all reservations in the project', args)
  )
  if return_code != 0:
    xpk_print(f'Get all reservations returned ERROR {return_code}')
    return 1
  return 0


def verify_reservation_exists(args) -> int:
  """Verify the reservation exists.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  command = (
        f'gcloud beta compute reservations describe {args.reservation}'
        f' --project={args.project} --zone={args.zone}'
    )
  return_code = (
      run_command_with_updates(
          command, 'Describe reservation', args)
  )
  if return_code != 0:
    xpk_print(f'Describe reservation returned ERROR {return_code}')
    xpk_print('Please confirm that your reservation name is correct.')
    return 1
  return 0


def get_capacity_arguments(args) -> tuple[str, int]:
  """Determine the TPU Nodepool creation capacity arguments needed.

  Args:
    args: user provided arguments for running the command.

  Returns:
    Tuple with string with the capacity argument to use and
    int of 0 if successful and 1 otherwise.
  """
  capacity_args = ""
  num_types = 0
  return_code = 0

  # Determine the capacity argument.
  if args.on_demand:
    capacity_args = ""
    num_types+=1
  if args.reservation:
    return_code = verify_reservation_exists(args)
    if return_code > 0:
      return capacity_args, return_code
    capacity_args = (
      f'--reservation-affinity=specific --reservation={args.reservation}'
    )
    num_types+=1
  if args.spot:
    capacity_args = "--spot"
    num_types+=1

  # Check that the number of arguments provided is valid.
  if num_types == 0:
    return_code = print_reservations(args)
    xpk_print(
      'ERROR: User needs to provide the capacity type. Please specify one of'
      ' the following `--reservation=$RESERVATION_NAME`, `--on-demand`'
      ' or `--spot`. See the above list of reservations to choose from.'
    )
    if return_code > 0:
      xpk_print('Listing all reservations failed!')
    return_code = 1
  elif num_types != 1:
    xpk_print(
      'ERROR: User specified more than one of the following arguments. Please'
      ' specify only one of `--reservation=$RESERVATION_NAME`, `--on-demand`'
      ' or `--spot`.'
    )
    return_code = 1

  return capacity_args, return_code


def get_user_input(input_msg):
  """Function to get the user input for a prompt.

  Args:
    input_msg: message to be displayed by the prompt.
  Returns:
    True if user enter y or yes at the prompt, False otherwise.
  """
  user_input = input(input_msg)
  return user_input in ('y', 'yes')


def run_gke_node_pool_create_command(args, system) -> int:
  """Run the Create GKE Node Pool request.

  Args:
    args: user provided arguments for running the command.
    system: System characteristics based on TPU type/topology.

  Returns:
    0 if successful and 1 otherwise.
  """
  device_type = args.tpu_type if args.tpu_type else args.device_type
  xpk_print(
      f'Creating {args.num_slices} node pool or pools of {device_type}\n'
      f'Underlyingly, we assume that means: {system}'
  )
  existing_node_pool_names, return_code = get_all_nodepools_programmatic(args)
  if return_code > 0:
    xpk_print('Listing all node pools failed!')
    return return_code
  desired_node_pool_names = [
      f'{args.cluster}-np-{slice_num}' for slice_num in range(args.num_slices)
  ]

  capacity_args, return_code = get_capacity_arguments(args)
  if return_code > 0:
    xpk_print('Parsing capacity arguments failed!')
    return return_code

  commands = []
  task_names = []
  for node_pool_name in desired_node_pool_names:
    if node_pool_name in existing_node_pool_names:
      continue
    command = (
        'gcloud beta container node-pools create'
        f' {node_pool_name} --node-version={args.gke_version}'
        f' --cluster={args.cluster}'
        f' --project={args.project} --node-locations={args.zone}'
        f' --region={zone_to_region(args.zone)}'
        f' --num-nodes={system.vms_per_slice}'
        f' --machine-type={system.gce_machine_type}'
        f' --host-maintenance-interval={args.host_maintenance_interval}'
        f' {capacity_args}'
        ' --scopes=storage-full,gke-default'
        ' --enable-gvnic --max-pods-per-node 15'
    )
    if system.accelerator_type == AcceleratorType['TPU']:
      command += (' --placement-type=COMPACT ')
      command += (f' --tpu-topology={system.topology}')
      command += (f' {args.custom_tpu_nodepool_arguments}')
    elif system.accelerator_type == AcceleratorType['GPU']:
      command += (' --placement-type=COMPACT ')
      command += f' --accelerator type={system.gke_accelerator},count={str(system.chips_per_vm)}'
    task = f'NodepoolCreate-{node_pool_name}'
    commands.append(command)
    task_names.append(task)

  node_pools_to_delete = []
  for existing_node_pool_name in existing_node_pool_names:
    if (
        existing_node_pool_name.find(f'{args.cluster}-np-') == 0
        and existing_node_pool_name not in desired_node_pool_names
    ):
      node_pools_to_delete.append(existing_node_pool_name)

  will_delete = True
  if node_pools_to_delete and not args.force:
    will_delete = get_user_input(
      f'Planning to delete {len(node_pools_to_delete)} node pools including '
      f'{node_pools_to_delete}. \nDo you wish to delete: y (yes) / n (no):\n')

  if not will_delete:
    xpk_print('Skipping delete commands. Continuing to next step.')
  else:
    for existing_node_pool_name in node_pools_to_delete:
      if (
          existing_node_pool_name.find(f'{args.cluster}-np-') == 0
          and existing_node_pool_name not in desired_node_pool_names
      ):
        command = (
            'gcloud beta container node-pools delete'
            f' {existing_node_pool_name} --cluster={args.cluster}'
            f' --zone={zone_to_region(args.zone)}'
            f' --project={args.project} --quiet'
        )
        task = f'Nodepool-Delete-{existing_node_pool_name}'
        commands.append(command)
        task_names.append(task)

  for i, command in enumerate(commands):
    xpk_print(f'To complete {task_names[i]} we are executing {command}')
  max_return_code = run_commands(
      commands, 'Create and Delete Nodepools', task_names, dry_run=args.dry_run
  )
  if max_return_code != 0:
    xpk_print(f'Create and Delete Nodepools returned ERROR {max_return_code}')
    return 1

  xpk_print('Create or delete node pool request complete.')
  return 0


def run_gke_cluster_delete_command(args) -> int:
  """Run the Delete GKE Cluster request.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  command = (
      'gcloud beta container clusters delete'
      f' {args.cluster} --project={args.project} --region={zone_to_region(args.zone)} --quiet'
  )

  return_code = run_command_with_updates(command, 'Cluster Delete', args)

  if return_code != 0:
    xpk_print(f'Cluster delete request returned ERROR {return_code}')
    return 1

  return 0


def run_gke_clusters_list_command(args) -> int:
  """List GKE Clusters within the project and location.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  command = (
      'gcloud container clusters list'
      f' --project={args.project} --region={zone_to_region(args.zone)}'
  )
  return_code = run_command_with_updates(command, 'Cluster List', args)
  if return_code != 0:
    xpk_print(f'Cluster list request returned ERROR {return_code}')
    return 1

  return 0


def set_cluster_command(args) -> int:
  """Run cluster configuration command to set the kubectl config.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  command = (
      'gcloud container clusters get-credentials'
      f' {args.cluster} --region={zone_to_region(args.zone)} --project={args.project} &&'
      ' kubectl config view && kubectl config set-context --current --namespace=default'
  )
  return_code = run_command_with_updates(
      command, 'Set Cluster', args, verbose=False
  )

  if return_code != 0:
    xpk_print(f'Set Cluster request returned ERROR {return_code}')
    return 1

  return 0


def install_kueue_on_cluster(args) -> int:
  """Install Kueue on the cluster.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  command = (
      'kubectl apply -f'
      ' https://github.com/kubernetes-sigs/kueue/releases/download/v0.4.1/manifests.yaml'
  )
  return_code = run_command_with_updates(command, 'Set Kueue On Cluster', args)

  if return_code != 0:
    xpk_print(f'Set Cluster request returned ERROR {return_code}')
    return 1

  return 0


def enable_kueue_crds(args, system) -> int:
  """Enable Kueue crds.

  Args:
    args: user provided arguments for running the command.
    system: system level arguments.

  Returns:
    0 if successful and 1 otherwise.
  """

  device_type = args.tpu_type if args.tpu_type else args.device_type
  cluster_hardware_name = f'{args.num_slices}x{device_type}'
  total_chips = args.num_slices * system.vms_per_slice * system.chips_per_vm
  yml_string = cluster_set_crd_yaml.format(
      system=system,
      cluster_hardware_name=cluster_hardware_name,
      total_chips=total_chips,
      accelerator_label=create_accelerator_label(system.accelerator_type, system),
      machine_label=create_machine_label(system.accelerator_type, system),
      resource_type=AcceleratorTypeToAcceleratorCharacteristics[system.accelerator_type].resource_type
  )
  tmp = write_temporary_file(yml_string)
  command = f'kubectl apply -f {str(tmp.file.name)}'
  # For kueue setup, we see a timeout error due to the webhook not
  # being ready. Let's retry and wait a few seconds.
  retry_limit = 5
  i = 0
  return_code = -1
  while (return_code != 0 and i < retry_limit):
    time.sleep(5)
    i += 1
    xpk_print(f'Try {i}: Applying Kueue CRDs')
    return_code = run_command_with_updates(command, 'Applying Kueue CRDs', args)

  if return_code != 0:
    xpk_print(f'Applying Kueue CRDS returned ERROR {return_code}')
    return return_code
  return 0


# TODO(vbarr): Remove this function when jobsets gets enabled by default on
# GKE clusters.
def set_jobset_on_cluster(args) -> int:
  """Add jobset command on server side and ask user to verify it is created.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  command = (
      'kubectl apply --server-side -f'
      ' https://github.com/kubernetes-sigs/jobset/releases/download/v0.3.1/manifests.yaml'
  )
  return_code = run_command_with_updates(command, 'Set Jobset On Cluster', args)

  if return_code != 0:
    xpk_print(
        'jobset command on server side returned with ERROR returncode'
        f' {return_code}.\n'
    )
    xpk_print(
      'This likely means you\'re missing Kubernetes Permissions, you can'
      ' validate this by checking if the error references permission'
      ' problems such as `requires one of ["container.*"] permission(s)`.'
      ' Follow our readme: https://github.com/google/xpk/blob/main/README.md#troubleshooting'
      ' for instructions on how to fix these permissions.'
    )
    return 1
  return 0


################### Subcommand Functions ###################
def default_subcommand_function(_args) -> int:  # args is unused, so pylint: disable=invalid-name
  """Default subcommand function.

  Args:
    _args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  xpk_print('Welcome to XPK! See below for overall commands:', flush=True)
  parser.print_help()
  cluster_parser.print_help()
  workload_parser.print_help()
  return 0


def cluster_create(args) -> int:
  """Function around cluster creation.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  system, return_code = get_system_characteristics(args)

  if return_code > 0:
    xpk_print('Fetching system characteristics failed!')
    xpk_exit(return_code)

  xpk_print(f'Starting cluster create for cluster {args.cluster}:', flush=True)
  add_zone_and_project(args)

  create_cluster_command_code = create_cluster_if_necessary(args)
  if create_cluster_command_code != 0:
    xpk_exit(create_cluster_command_code)

  run_gke_node_pool_create_command_code = run_gke_node_pool_create_command(
      args, system
  )
  if run_gke_node_pool_create_command_code != 0:
    xpk_exit(run_gke_node_pool_create_command_code)

  set_cluster_command_code = set_cluster_command(args)
  if set_cluster_command_code != 0:
    xpk_exit(set_cluster_command_code)

  xpk_print(
      'Enabling the jobset API on our cluster, to be deprecated when Jobset is'
      ' globally available'
  )
  set_jobset_on_cluster_code = set_jobset_on_cluster(args)
  if set_jobset_on_cluster_code != 0:
    xpk_exit(set_jobset_on_cluster_code)

  xpk_print('Enabling Kueue on the cluster')
  install_kueue_on_cluster_code = install_kueue_on_cluster(args)
  if install_kueue_on_cluster_code != 0:
    xpk_exit(install_kueue_on_cluster_code)

  xpk_print('Enable Kueue CRDs')
  enable_kueue_creds_code = enable_kueue_crds(args, system)
  if enable_kueue_creds_code != 0:
    xpk_exit(enable_kueue_creds_code)

  xpk_print('Creating ConfigMap for cluster')
  create_cluster_configmap_code = create_cluster_configmap(args, system)
  if create_cluster_configmap_code != 0:
    xpk_exit(create_cluster_configmap_code)

  xpk_print('GKE commands done! Resources are created.')
  xpk_print(
      'See your GKE Cluster here:'
      # pylint: disable=line-too-long
      f' https://console.cloud.google.com/kubernetes/clusters/details/{zone_to_region(args.zone)}/{args.cluster}/details?project={args.project}'
  )
  xpk_exit(0)


def cluster_delete(args) -> int:
  """Function around cluster delete.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  xpk_print(f'Starting cluster delete for cluster: {args.cluster}', flush=True)
  add_zone_and_project(args)
  run_gke_cluster_delete_command_code = run_gke_cluster_delete_command(args)
  if run_gke_cluster_delete_command_code != 0:
    xpk_exit(run_gke_cluster_delete_command_code)
  xpk_print(f'GKE commands done! Cluster {args.cluster} deleted.\n')
  return 0


def cluster_cacheimage(args) -> int:
  """Function around cluster cacheimage.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  xpk_print(
      f'Starting cluster cacheimage for cluster: {args.cluster}', flush=True
  )
  add_zone_and_project(args)

  set_cluster_command_code = set_cluster_command(args)
  if set_cluster_command_code != 0:
    xpk_exit(set_cluster_command_code)
  system, return_code = get_system_characteristics(args)

  if return_code > 0:
    xpk_print('Fetching system characteristics failed!')
    xpk_exit(return_code)

  node_selector_key = AcceleratorTypeToAcceleratorCharacteristics[system.accelerator_type].accelerator_label
  yml_string = cluster_preheat_yml.format(
      cachekey=args.cache_key,
      image_name=args.docker_image,
      nodeSelectorKey=node_selector_key
  )
  tmp = write_temporary_file(yml_string)
  command_apply = f'kubectl apply -f {str(tmp.file.name)}'
  command_delete = (
      f'kubectl delete -f {str(tmp.file.name)} --ignore-not-found=true'
  )

  return_code = run_command_with_updates(
      command_delete, 'Deleting Cached Image', args
  )
  if return_code != 0:
    xpk_print(f'Delete Cached Image returned ERROR {return_code}')
    xpk_exit(return_code)

  return_code = run_command_with_updates(
      command_apply, 'Creating Cached Image', args
  )
  if return_code != 0:
    xpk_print(f'Create Cached Image returned ERROR {return_code}')
    xpk_exit(return_code)
  xpk_exit(0)


def cluster_describe(args) -> int:
  """Function around cluster describe.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  xpk_print(f'Starting nodepool list for cluster: {args.cluster}', flush=True)
  add_zone_and_project(args)

  set_cluster_command_code = set_cluster_command(args)
  if set_cluster_command_code != 0:
    xpk_exit(set_cluster_command_code)

  command = (
      f'gcloud container node-pools  list --cluster {args.cluster} '
      f'--project={args.project} --region={zone_to_region(args.zone)}'
  )

  return_code = run_command_with_updates(command, 'Cluster nodepool list', args)
  if return_code != 0:
    xpk_exit(return_code)

  return_code_node_output, node_output = run_command_for_value(
      r"kubectl get node --no-headers=true --selector='cloud.google.com/gke-tpu-accelerator' | wc -l",
      'Count TPU Nodes',
      args,
  )
  if return_code_node_output != 0:
    xpk_exit(return_code_node_output)
  number_tpu_vms_in_cluster = int(node_output)

  return_code_pod_output, pod_output = run_command_for_value(
      "kubectl get pod -o=custom-columns='Status:.status.phase' | grep -i"
      ' Running | wc -l',
      'Count TPU Pods',
      args,
  )
  if return_code_pod_output != 0:
    xpk_exit(return_code_pod_output)
  number_tpu_pods_in_cluster = int(pod_output)

  xpk_print(
      f'The cluster contains {number_tpu_vms_in_cluster} TPUVMs of which'
      f' {number_tpu_pods_in_cluster} are in use.'
  )

  xpk_print('GKE commands done!\n')
  return 0


def cluster_list(args) -> int:
  """Function around cluster list.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  add_zone_and_project(args)
  xpk_print(f'For project {args.project} and zone {args.zone}:', flush=True)
  if run_gke_clusters_list_command(args):
    return 1
  return 0


def validate_docker_image(docker_image, args) -> int:
  """Validates that the user provided docker image exists in your project.

  Args:
    docker_image: The docker image to verify.
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """

  project = args.project

  if docker_image.find('gcr.io') == -1:
    return 0

  command = (
      f'gcloud container images describe {docker_image} --project {project}'
  )
  return_code = run_command_with_updates(
      command, 'Validate Docker Image', args, verbose=False
  )
  if return_code != 0:
    xpk_print(
        'Failed to validate your docker image, check the docker image. You'
        f' should be able to navigate to the URL {docker_image} in {project}'
    )
    return return_code
  else:
    return 0


def build_docker_image_from_base_image(args, verbose=True) -> tuple[int, str]:
  """Adds script dir to the base docker image and uploads the image.

  Args:
    args: user provided arguments for running the command.

  Returns:
    Tuple of:
      0 if successful and 1 otherwise.
      Name of the Docker image created.
  """

  # Pick a name for the docker image.
  docker_image_prefix = os.getenv('USER', 'unknown')
  docker_name = f'{docker_image_prefix}-runner'

  docker_file = script_dir_dockerfile.format(
      base_docker_image=args.base_docker_image,
  )
  tmp = write_temporary_file(docker_file)
  docker_build_command = (
      f'docker build -f {str(tmp.file.name)} -t {docker_name}'
      f' {args.script_dir}'
  )
  xpk_print(f'Building {args.script_dir} into docker image.')
  return_code = run_command_with_updates(
      docker_build_command, 'Building script_dir into docker image', args,
      verbose=verbose
  )
  if return_code != 0:
    xpk_print(
        'Failed to add script_dir to docker image, check the base docker image.'
        f' You should be able to navigate to the URL {args.base_docker_image}'
        f' in {args.project}.'
    )
    xpk_exit(1)

  # Pick a randomly generated `tag_length` character docker tag.
  tag_length = 4
  tag_random_prefix = ''.join(random.choices(string.ascii_lowercase, k=tag_length))
  tag_datetime = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
  tag_name = f'{tag_random_prefix}-{tag_datetime}'
  cloud_docker_image = f'gcr.io/{args.project}/{docker_name}:{tag_name}'
  xpk_print(f'Adding Docker Image: {cloud_docker_image} to {args.project}')

  # Tag the docker image.
  tag_docker_image_command = (
      f'docker tag {docker_name} {cloud_docker_image}'
  )
  return_code = run_command_with_updates(
      tag_docker_image_command, 'Tag Docker Image', args,
      verbose=verbose
  )
  if return_code != 0:
    xpk_print(
        f'Failed to tag docker image with tag: {tag_name}.'
        f' You should be able to navigate to the URL {cloud_docker_image} in'
        f' {args.project}.'
    )
    xpk_exit(1)

  # Upload image to Artifact Registry.
  upload_docker_image_command = (
      f'docker push {cloud_docker_image}'
  )
  return_code = run_command_with_updates(
      upload_docker_image_command, 'Upload Docker Image', args,
      verbose=verbose
  )
  if return_code != 0:
    xpk_print(
        f'Failed to upload docker image.'
        f' You should be able to navigate to the URL {cloud_docker_image} in'
        f' {args.project}.'
    )
    xpk_exit(1)
  return return_code, cloud_docker_image


def check_if_workload_exists(args) -> bool:
  """Check if workload exists.

  Args:
     args: user provided arguments for running the command.

  Returns:
    returns true if workload exist, otherwise returns false.
  """
  columns = {
      'Jobset': '.metadata.ownerReferences[0].name',
  }

  s = ','.join([key + ':' + value for key, value in columns.items()])

  command = f"kubectl get workloads -o=custom-columns='{s}'"
  return_code, return_msg = run_command_for_value(
      command, 'Check if Workload Already Exists', args
  )

  if return_code != 0:
    xpk_print(f'List Job request returned ERROR {return_code}')
    xpk_exit(return_code)

  lines = return_msg.split('\n')
  new_workload_name = args.workload
  for line in lines:
    if line == new_workload_name:
      return True
  return False


# TODO: Update when GPU support is enabled
def check_if_workload_can_schedule(args, system):
  """Check if workload can schedule based on the cluster resources (tpu_type and maximum VM in cluster).

  Args:
    args: user provided arguments for running the command.
    system: system characteristics

  Returns:
    returns true if workload can schedule, otherwise returns false.
  """
  cluster_config_map = get_cluster_configmap(args)

  # Prevents workload creation failure for existing clusters with no ConfigMap
  if cluster_config_map is None:
    xpk_print(f'No ConfigMap exist for cluster with the name {args.cluster}-configmap.')
    return True

  device_type = args.tpu_type if args.tpu_type else args.device_type
  if device_type not in cluster_config_map:
    xpk_print(f'{args.workload} is requesting {device_type} but '
      f'cluster only contains {cluster_config_map.keys()}. '
      'XPK will not create this workload.'
    )
    return False

  max_vm_in_cluster = cluster_config_map[device_type]
  vm_required_by_workload = int(args.num_slices) * system.vms_per_slice
  if vm_required_by_workload > max_vm_in_cluster:
    xpk_print(
        f'{args.workload} is requesting {args.num_slices} slice/slices of {device_type}, '
        f'which is {vm_required_by_workload} VMs, '
        f'but the cluster only contains {max_vm_in_cluster} VMs of {device_type}. '
        'XPK will not create this workload.'
    )
    return False

  return True


def use_base_docker_image_or_docker_image(args) -> bool:
  """Checks for correct docker image arguments.

  Args:
    args: user provided arguments for running the command.

  Returns:
    True if intended to use base docker image, False to use docker image.
  """
  use_base_docker_image = True
  # Check if (base_docker_image and script_dir) or (docker_image) is set.
  if args.docker_image is not None:
    if args.script_dir is not default_script_dir:
      xpk_print(
          '`--script-dir` and --docker-image can not be used together. Please'
          ' see `--help` command for more details.'
      )
      xpk_exit(1)
    if args.base_docker_image is not default_docker_image:
      xpk_print(
          '`--base-docker-image` and --docker-image can not be used together.'
          ' Please see `--help` command for more details.'
      )
      xpk_exit(1)
    use_base_docker_image = False
  return use_base_docker_image


def setup_docker_image(args) -> tuple[int, str]:
  """Does steps to verify docker args, check image, and build image (if asked).

  Args:
    args: user provided arguments for running the command.

  Returns:
    tuple:
      0 if successful and 1 otherwise.
      Name of the docker image to use.
  """
  use_base_docker_image = use_base_docker_image_or_docker_image(args)

  docker_image = args.base_docker_image
  if use_base_docker_image:
    validate_docker_image_code = validate_docker_image(
        docker_image, args
    )
    if validate_docker_image_code != 0:
      xpk_exit(validate_docker_image_code)
    build_docker_image_code, docker_image = build_docker_image_from_base_image(args)
    if build_docker_image_code != 0:
      xpk_exit(build_docker_image_code)
  else:
    docker_image = args.docker_image
    validate_docker_image_code = validate_docker_image(
        args.docker_image, args
    )
    if validate_docker_image_code != 0:
      xpk_exit(validate_docker_image_code)

  return 0, docker_image

def get_main_and_sidecar_container(args, system, docker_image, command) -> str:
  """Generate yaml for main and sidecar container.
  Args:
    args: user provided arguments for running the command.
    system: system characteristics
    docker_image: docker image
    command: command to run in the main container

  Returns:
    str:
      yaml for main and sidecar container
  """
  resource_type = AcceleratorTypeToAcceleratorCharacteristics[system.accelerator_type].resource_type
  main_container = get_main_container(args, system, docker_image, command, resource_type)
  yaml = """- name: stacktrace-explorer
                image: busybox:1.28
                args: [/bin/sh, -c, "while [ ! -d /tmp/debugging ]; do sleep 60; done; while [ ! -e /tmp/debugging/* ]; do sleep 60; done; tail -n+1 -f /tmp/debugging/*"]
                volumeMounts:
                - name: tpu-stack-trace
                  readOnly: true
                  mountPath: /tmp/debugging
              {main_container}
                volumeMounts:
                - name: tpu-stack-trace
                  mountPath: /tmp/debugging
              volumes:
              - name: tpu-stack-trace
  """
  return yaml.format(main_container=main_container)

def get_main_container(args, system, docker_image, command, resource_type) -> str:
  """Generate yaml for main container.
  Args:
    args: user provided arguments for running the command.
    system: system characteristics
    docker_image: docker image
    command: command to run in the main container

  Returns:
    str:
      yaml for main container
  """
  yaml = """- name: {args.docker_name}
                image: {docker_image}
                env: {args.env}
                ports:
                - containerPort: 8471
                - containerPort: 8080
                {jax_coordinator_port}
                securityContext:
                  privileged: true
                command:
                - bash
                - -c
                - |
                  echo XPK Start: $(date) ; _sigterm() ( kill -SIGTERM $!;); trap _sigterm SIGTERM; ({command}) & PID=$!; while kill -0 $PID 2>/dev/null; do sleep 5; done; EXIT_CODE=$? ; echo XPK End: $(date); echo EXIT_CODE=$EXIT_CODE
                resources:
                  limits:
                    {resource_type}: {system.chips_per_vm}
  """
  return yaml.format(args=args,
                   system=system,
                   jax_coordinator_port=add_jax_coordinator_port(system),
                   docker_image=docker_image,
                   command=command,
                   resource_type=resource_type)

def add_jax_coordinator_port(system):
  """Add jax coordinator port only for CPUs

  Args:
    system: system characteristics.

  Returns:
    str:
      jax coordinator port as a YAML string
  """
  if system.accelerator_type == AcceleratorType['CPU']:
    return '- containerPort: 1234'
  return ""

def get_gke_dashboard(args, dashboard_filter):
  """Get the identifier of GKE dashboard deployed in the project.

  Args:
    args: user provided arguments for running the command.

  Returns:
    bool:
      True if 'gcloud monitoring dashboards list' returned an error or
      multiple dashboards with same filter exist in the project,
      False otherwise.
    str:
      identifier of dashboard if deployed in project,
      None otherwise.
  """
  command = (
      'gcloud monitoring dashboards list'
      f' --project={args.project} --filter="{dashboard_filter}" --format="value(name)" --verbosity=error'
  )

  return_code, return_value = run_command_for_value(command, 'GKE Dashboard List', args)

  if return_code != 0:
    xpk_print(f'GKE Dashboard List request returned ERROR {return_code}. '
              'If there is a permissions error, please check '
              'https://github.com/google/xpk/blob/main/README.md#roles-needed-based-on-permission-errors '
              'for possible solutions.')
    return True, None

  if not return_value:
    xpk_print(
        f'No dashboard with {dashboard_filter} found in the project:{args.project}.'
    )
    return False, return_value

  dashboards = return_value.strip().split('\n')
  if len(dashboards) > 1:
    xpk_print(
      f'Multiple dashboards with same {dashboard_filter} exist in the project:{args.project}. '
      'Delete all but one dashboard deployed using https://github.com/google/cloud-tpu-monitoring-debugging.'
    )
    return True, None

  if dashboards[0]:
    return False, dashboards[0].strip().split('/')[-1]

  return True, None

def get_gke_outlier_dashboard(args):
  """Get the identifier of GKE outlier dashboard deployed in the project.

  Args:
    args: user provided arguments for running the command.

  Returns:
    str:
      identifier of outlier dashboard if deployed in project,
      None otherwise.
  """
  outlier_dashboard_filter = "displayName:'GKE - TPU Monitoring Dashboard'"
  is_error, dashboard_id = get_gke_dashboard(args, outlier_dashboard_filter)

  # 'gcloud monitoring dashboards list' returned an error or multiple dashboards with same filter exist in the project
  if is_error:
    return None

  # 'gcloud monitoring dashboards list' succeeded but no dashboard for the filter exist in the project
  if not is_error and not dashboard_id:
    xpk_print(
        'Follow https://github.com/google/cloud-tpu-monitoring-debugging'
        ' to deploy monitoring dashboard to view statistics and outlier mode of GKE metrics.'
    )
    return None

  return dashboard_id

def get_gke_debugging_dashboard(args):
  """Get the identifier of GKE debugging dashboard deployed in the project.

  Args:
    args: user provided arguments for running the command.

  Returns:
    str:
      identifier of debugging dashboard if deployed in project,
      None otherwise.
  """
  debugging_dashboard_filter = "displayName:'GKE - TPU Logging Dashboard'"
  is_error, dashboard_id = get_gke_dashboard(args, debugging_dashboard_filter)

  # 'gcloud monitoring dashboards list' returned an error or multiple dashboards with same filter exist in the project
  if is_error:
    return None

  # 'gcloud monitoring dashboards list' succeeded but no dashboard for the filter exist in the project
  if not is_error and not dashboard_id:
    xpk_print(
        'Follow https://github.com/google/cloud-tpu-monitoring-debugging'
        ' to deploy debugging dashboard to view stack traces collected in Cloud Logging.'
    )
    return None

  return dashboard_id

def create_accelerator_label(accelerator_type, system) -> str:
  """Generates accelerator label.

  Args:
    accelerator_type: type of accelerator.
    system: system characteristics.

  Returns:
    The accelerator label.
  """
  if accelerator_type == AcceleratorType['CPU']:
    return ""
  return f"{AcceleratorTypeToAcceleratorCharacteristics[accelerator_type].accelerator_label}: {system.gke_accelerator}"

def create_machine_label(accelerator_type, system) -> str:
  """Generates machine label.

  Args:
    accelerator_type: type of accelerator.
    system: system characteristics.

  Returns:
    The machine label.
  """
  if accelerator_type == AcceleratorType['TPU']:
    return f"{AcceleratorTypeToAcceleratorCharacteristics[accelerator_type].machine_label}: {system.topology}"
  return ""

def calculate_process_count(num_slices, vms_per_slice) -> str:
  """ Calculates the total number of processes in the workload.
  Args:
    num_slices: Number of slices to be used in the workload.
    vms_per_slice: number of VMs in each slice.

  Returns:
    str: total number of processes. 
  """
  num_processes = int(num_slices) * int(vms_per_slice)
  return f"{num_processes}"

def get_cpu_env(num_slices, system) -> str:
  """Generate environment variables for CPU nodepools
  Args:
    num_slices: Number of slices to be used in the workload.
    system: system characteristics

  Returns:
    str: yaml containing env variables 
  """
  yaml = """env:
                - name: REPLICATED_JOB_NAME
                  valueFrom:
                    fieldRef:
                      fieldPath: metadata.annotations['jobset.sigs.k8s.io/replicatedjob-name']
                - name: JOBSET_NAME
                  valueFrom:
                    fieldRef:
                      fieldPath: metadata.annotations['jobset.sigs.k8s.io/jobset-name']
                - name: JAX_COORDINATOR_ADDRESS
                  value: "$(JOBSET_NAME)-$(REPLICATED_JOB_NAME)-0-0.$(JOBSET_NAME)"
                - name: JOB_INDEX
                  valueFrom:
                    fieldRef:
                      fieldPath: metadata.annotations['jobset.sigs.k8s.io/job-index']
                - name: JOB_COMPLETION_INDEX
                  valueFrom:
                    fieldRef:
                      fieldPath: metadata.annotations['batch.kubernetes.io/job-completion-index']
                - name: PROCESSES_IN_JOB
                  value: "{processes_in_job}"
                - name: JAX_PROCESS_COUNT
                  value: "{process_count}"
  """
  if system.accelerator_type == AcceleratorType['CPU']:
    return yaml.format(processes_in_job = system.vms_per_slice,
                      process_count=calculate_process_count(num_slices,system.vms_per_slice))
  return ""

def get_cpu_affinity(accelerator_type) -> str:
  """Generate affinity rules for CPU nodepools, so that workload pods are 
  not scheduled on the default pool machines.
  Args:
    accelerator_type: TPU / GPU / CPU

  Returns:
    str: yaml containing affinity constraints 
  """
  yaml = """affinity:
                nodeAffinity:
                  requiredDuringSchedulingIgnoredDuringExecution:
                    nodeSelectorTerms:
                    - matchExpressions:
                      - key: cloud.google.com/gke-nodepool
                        operator: NotIn
                        values:
                        - default-pool
  """
  if accelerator_type == AcceleratorType['CPU']:
    return yaml
  return ""

def get_system_characteristics(args) -> tuple[SystemCharacteristics|None, int]:
  """Get system characteristics based on user provided arguments.

  Args:
    args: user provided arguments for running the command.

  Returns:
    Tuple with string with the system characteristics and
    int of 0 if successful and 1 otherwise.
  """
  device_type = args.tpu_type if args.tpu_type else args.device_type
  if device_type in UserFacingNameToSystemCharacteristics:
    return UserFacingNameToSystemCharacteristics[device_type], 0
  else:
    return None, 1

def workload_create(args) -> int:
  """Run jobset apply command for a file.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  add_zone_and_project(args)

  set_cluster_command_code = set_cluster_command(args)
  if set_cluster_command_code != 0:
    xpk_exit(set_cluster_command_code)

  workload_exists = check_if_workload_exists(args)

  if workload_exists:
    xpk_print(
        f'{args.workload} already exist, XPK will not create this workload.'
        ' Please pick a new workload name'
    )
    xpk_exit(1)

  xpk_print('Starting workload create', flush=True)
  system, return_code = get_system_characteristics(args)

  if return_code > 0:
    xpk_print('Fetching system characteristics failed!')
    xpk_exit(return_code)

  if not check_if_workload_can_schedule(args, system):
    xpk_exit(1)

  xpk_print('Starting workload create', flush=True)

  setup_docker_image_code, docker_image = setup_docker_image(args)
  if setup_docker_image_code != 0:
    xpk_exit(setup_docker_image_code)

  add_env_config(args)
  command = args.command
  if args.debug_dump_gcs:
    command += ('; WORKER_ID=$HOSTNAME;'
                f'gsutil cp -r /tmp/xla_dump/ {args.debug_dump_gcs}/$WORKER_ID')

  debugging_dashboard_id = None
  resource_type = AcceleratorTypeToAcceleratorCharacteristics[system.accelerator_type].resource_type
  if system.accelerator_type == AcceleratorType['TPU'] and args.deploy_stacktrace_sidecar:
    xpk_print('Sidecar container to display stack traces for TPU workloads will also be deployed.')
    container = get_main_and_sidecar_container(args, system, docker_image, command)
    # Get GKE debugging dashboard only when sidecar container is deployed for TPU workloads
    debugging_dashboard_id = get_gke_debugging_dashboard(args)
  else:
    container = get_main_container(args, system, docker_image, command, resource_type)

  yml_string = workload_create_yaml.format(args=args,
                                           system=system,
                                           docker_image=docker_image,
                                           command=command,
                                           container=container,
                                           affinity=get_cpu_affinity(system.accelerator_type),
                                           env=get_cpu_env(args.num_slices,system),
                                           accelerator_label=create_accelerator_label(system.accelerator_type, system),
                                           machine_label=create_machine_label(system.accelerator_type, system),
                                           resource_type=resource_type)
  tmp = write_temporary_file(yml_string)
  command = f'kubectl apply -f {str(tmp.file.name)}'

  return_code = run_command_with_updates(command, 'Creating Workload', args)

  if return_code != 0:
    xpk_print(f'Create Workload request returned ERROR {return_code}')
    xpk_exit(return_code)

  # Get GKE outlier dashboard for TPU
  outlier_dashboard_id = None
  if system.accelerator_type == AcceleratorType['TPU']:
    outlier_dashboard_id = get_gke_outlier_dashboard(args)

  xpk_print(
      'Follow your workload here:'
      # pylint: disable=line-too-long
      f' https://console.cloud.google.com/kubernetes/service/{zone_to_region(args.zone)}/{args.cluster}/default/{args.workload}/details?project={args.project}'
  )

  if outlier_dashboard_id is not None:
    xpk_print(
        'Check statistics and outlier mode of GKE metrics here:'
        # pylint: disable=line-too-long
        f' https://console.cloud.google.com/monitoring/dashboards/builder/{outlier_dashboard_id}?project={args.project}&f.rlabel.cluster_name.ClusterName={args.cluster}.'
        f' To view the metric data for your workload, select {args.workload} from the JobName filter on the dashboard.'
    )

  if debugging_dashboard_id is not None:
    xpk_print(
        'Check stack traces collected in Cloud Logging here:'
        # pylint: disable=line-too-long
        f' https://console.cloud.google.com/monitoring/dashboards/builder/{debugging_dashboard_id}?project={args.project}&f.rlabel.cluster_name.ClusterName={args.cluster}.'
        f' To view the stack traces for your workload, select {args.workload} from the JobName filter on the dashboard.'
    )

  xpk_exit(0)


def workload_delete(args) -> int:
  """Function around workload delete.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  xpk_print('Starting Workload delete', flush=True)
  add_zone_and_project(args)
  set_cluster_command_code = set_cluster_command(args)
  if set_cluster_command_code != 0:
    xpk_exit(set_cluster_command_code)

  will_delete = True
  if not args.workload:
    xpk_print("Get the name of the workloads in the cluster.")
    return_code, return_value = get_workload_list(args)

    if return_code != 0:
      xpk_print(f'List Job request returned ERROR {return_code}')
      xpk_exit(return_code)
    # Skip the header
    workloads = [x.split(' ')[0] for x in return_value.splitlines()][1:]
    if workloads and not args.force:
      will_delete = get_user_input(
        f'Planning to delete {len(workloads)} workloads in the cluster {args.cluster} '
        f'including {workloads}. \nDo you wish to delete: y (yes) / n (no):\n')
  else:
    workloads = [args.workload]

  if not workloads:
    xpk_print("There are no workloads to delete matching the filter in the cluster.")
  elif not will_delete:
    xpk_print("Skipping delete command.")
  else:
    for workload in workloads:
      args.workload = workload
      yml_string = workload_delete_yaml.format(args=args)
      tmp = write_temporary_file(yml_string)
      command = f'kubectl delete -f {str(tmp.file.name)}'
      return_code = run_command_with_updates(command, 'Delete Workload', args)

      if return_code != 0:
        xpk_print(f'Delete Workload request returned ERROR {return_code}')
        xpk_exit(return_code)
  xpk_exit(0)


def workload_list_awk_command(filter_key) -> str:
  """Function returns the awk command needed from the filter specified.

  Args:
    filter_key: workload list filter to awk against

  Returns:
    awk command to use in filtering workload list.
  """

  return f' | awk -e \'NR == 1 || {filter_key} {{print $0}}\''


def determine_workload_list_filter_by_status(args) -> str:
  """Function to create the filtered view of workload list.

  Args:
    args: user provided arguments for running the command.

  Returns:
    the argument needed to filter by status of jobs in workload list.
  """
  # Argument positions related to columns created by workload list command.
  status_arg='$7'
  running_vms_arg='$5'
  status_verbose_arg='$9'
  if args.filter_by_status == 'EVERYTHING':
    return ''
  elif args.filter_by_status == 'RUNNING':
    # Running includes the status Admitted or Evicted, and when the number of
    # vms running is > 0.
    return workload_list_awk_command(
        f'({status_arg} ~ \"Admitted|Evicted\" && {running_vms_arg} ~ /^[0-9]+$/ && {running_vms_arg} > 0)'
    )
  elif args.filter_by_status == 'QUEUED':
    # Queued includes the status Admitted or Evicted, and when the number of
    # vms running is 0.
    return workload_list_awk_command(
        f'({status_arg} ~ \"Admitted|Evicted\" && ({running_vms_arg} ~ \"<none>\" || {running_vms_arg} == 0))'
    )
  elif args.filter_by_status == 'FINISHED':
    return workload_list_awk_command(f'{status_arg} == \"Finished\"')
  elif args.filter_by_status == 'FAILED':
    # Failed includes the status Finished, and when the verbose reason is failed.
    return workload_list_awk_command(f'({status_arg} == \"Finished\" && {status_verbose_arg} ~ \"failed\")')
  elif args.filter_by_status == 'SUCCESSFUL':
    # Failed includes the status Finished, and when the verbose reason is finished/success.
    return workload_list_awk_command(f'({status_arg} == \"Finished\" && {status_verbose_arg} ~ \"finished\")')
  raise RuntimeError(f'Can not find filter type: {args.filter_by_status}')


def determine_workload_list_filter_by_job(args) -> str:
  """Function to filter view of workload list based on job name.

  Args:
    args: user provided arguments for running the command.

  Returns:
    the argument needed to filter job names from workload list
  """
  # Argument positions related to columns created by workload list command.
  if not args.filter_by_job:
    return ''
  else:
    job_name_arg="$1"
    return workload_list_awk_command(f'{job_name_arg} ~ \"{args.filter_by_job}\"')


def get_workload_list(args) -> None:
  """Function to get the list of the workloads in the cluster.

  Args:
    args: user provided arguments for running the command.

  Returns:
    return_code: 0 if successful and 1 otherwise.
    return_value: workloads in the cluster matching the criteria.
  """
  columns = {
      'Jobset Name': '.metadata.ownerReferences[0].name',
      'Created Time': '.metadata.creationTimestamp',
      'Priority': '.spec.priorityClassName',
      'TPU VMs Needed': '.spec.podSets[0].count',
      'TPU VMs Running/Ran': '.status.admission.podSetAssignments[-1].count',
      'TPU VMs Done': '.status.reclaimablePods[0].count',
      'Status': '.status.conditions[-1].type',
      'Status Message': '.status.conditions[-1].message',
      'Status Time': '.status.conditions[-1].lastTransitionTime',
    }
  s = ','.join([key + ':' + value for key, value in columns.items()])

  workload_list_filter_status_cmd = determine_workload_list_filter_by_status(args)
  workload_list_filter_job_cmd = determine_workload_list_filter_by_job(args)
  command = (f'kubectl get workloads -o=custom-columns="{s}" '
             f'{workload_list_filter_status_cmd} {workload_list_filter_job_cmd}'
             )

  return_code, return_value = run_command_for_value(command, 'List Jobs', args)
  return return_code, return_value


def workload_list(args) -> None:
  """Function around workload list.

  Args:
    args: user provided arguments for running the command.

  Returns:
    0 if successful and 1 otherwise.
  """
  print(args)

  xpk_print('Starting workload list', flush=True)
  add_zone_and_project(args)
  set_cluster_command_code = set_cluster_command(args)
  if set_cluster_command_code != 0:
    xpk_exit(set_cluster_command_code)

  return_code, return_value = get_workload_list(args)

  if return_code != 0:
    xpk_print(f'List Job request returned ERROR {return_code}')
    xpk_exit(return_code)
  xpk_print(return_value)
  xpk_exit(0)


def add_shared_arguments(custom_parser):
  """Add shared arguments to the parser.

  Args:
    custom_parser: parser to add shared arguments to.
  """
  custom_parser.add_argument(
      '--project',
      type=str,
      default=None,
      help='GCE project name, defaults to "gcloud config project."',
  )
  custom_parser.add_argument(
      '--zone',
      type=str,
      default=None,
      help=(
          'GCE zone, e.g. us-central2-b, defaults to "gcloud config '
          'compute/zone." Only one of --zone or --region is allowed in a '
          'command.'
      ),
  )
  custom_parser.add_argument(
      '--dry-run',
      type=bool,
      action=argparse.BooleanOptionalAction,
      default=False,
      help=(
          'If given `--dry-run`, xpk will print the commands it wants to run'
          ' but not run them. This is imperfect in cases where xpk might'
          ' branch based on the output of commands'
      ),
  )


############### Define flags ###############
# Create top level parser for xpk command.
parser = argparse.ArgumentParser(description='xpk command', prog='xpk')

xpk_subcommands = parser.add_subparsers(
    title='xpk subcommands', dest='xpk_subcommands', help='Top level commands'
)
parser.set_defaults(func=default_subcommand_function)


def workload_name_type(value, pat=re.compile(r'[a-z]([-a-z0-9]*[a-z0-9])?')):
  """Validate that the workload name matches the expected pattern."""
  match = pat.fullmatch(value)
  if not match or len(match.group(0)) > 40:
    raise argparse.ArgumentTypeError(
        'Workload name must be less than 40 characters and match the pattern'
        f' `{pat.pattern}`'
        f' Name is currently {value}'
    )
  return value


def directory_path_type(value):
  if not os.path.isdir(value):
    raise argparse.ArgumentTypeError(
      f'Directory path is invalid. User provided path was {value}'
    )
  return value


#### "cluster" command parser. ####
cluster_parser = xpk_subcommands.add_parser(
    'cluster',
    help='Commands around creating, deleting, and viewing clusters.',
)
cluster_parser.set_defaults(func=default_subcommand_function)
cluster_subcommands = cluster_parser.add_subparsers(
    title='cluster subcommands',
    dest='xpk_cluster_subcommands',
    help=(
        'These are commands related to cluster management. Look at help for'
        ' specific subcommands for more details.'
    ),
)

### "cluster create" command parser ###
cluster_create_parser = cluster_subcommands.add_parser(
    'create', help='Create cloud clusters.'
)
cluster_create_required_arguments = cluster_create_parser.add_argument_group(
    'Required Arguments',
    'Arguments required for cluster create.',
)
cluster_create_optional_arguments = cluster_create_parser.add_argument_group(
    'Optional Arguments', 'Arguments optional for cluster create.'
)
cluster_create_capacity_arguments = cluster_create_parser.add_argument_group(
    'Capacity Arguments', 'Arguments related to capacity for cluster create.'
)

### Required arguments.
cluster_create_required_arguments.add_argument(
    '--cluster',
    type=str,
    default=None,
    help=(
        'The name of the cluster. Will be used as the prefix for internal'
        ' objects in the cluster.'
    ),
    required=True,
)

cluster_device_group = cluster_create_required_arguments.add_mutually_exclusive_group(required=True)

cluster_device_group.add_argument(
    '--tpu-type',
    type=str,
    default=None,
    help='The tpu type to use, v5litepod-16, etc.'
)
cluster_device_group.add_argument(
    '--device-type',
    type=str,
    default=None,
    help='The device type to use (can be tpu or gpu or cpu), v5litepod-16, h100-80gb-8, n2-standard-32-4 etc.'
)


# Capacity Arguments
cluster_create_capacity_arguments.add_argument(
    '--on-demand',
    action='store_true',
    help=(
        'Sets node pool creation to use on-demand resources. '
        ' See `--reservation` or `--spot` for other capacity types.'
    ),
)
cluster_create_capacity_arguments.add_argument(
    '--reservation',
    type=str,
    help=(
        'The reservation to be used for acquiring resources in the'
        ' cluster. This will attempt to find the provided reservation.'
        ' See `--spot` or `--on-demand` for other capacity types.'
    ),
)
cluster_create_capacity_arguments.add_argument(
    '--spot',
    action='store_true',
    help=(
        'Sets node pool creation to use spot resources.'
        ' See `--reservation` or `--on-demand` for other capacity types.'
    ),
)

### Optional Arguments
cluster_create_optional_arguments.add_argument(
    '--host-maintenance-interval',
    type=str,
    choices=['AS_NEEDED', 'PERIODIC'],
    default='AS_NEEDED',
    help='The maintenance policy of the cluster and respective clusters.',
)
cluster_create_optional_arguments.add_argument(
    '--gke-version',
    type=str,
    default=default_gke_version,
    help=(
        'The GKE version of the cluster and respective clusters. The default is'
        f' "{default_gke_version}".'
    ),
)
cluster_create_optional_arguments.add_argument(
    '--num-slices',
    type=int,
    default=1,
    help='The number of slices to run the job on, defaults to 1.',
    required=True,
)
cluster_create_optional_arguments.add_argument(
  '--default-pool-cpu-machine-type',
    type=str,
    default='e2-standard-16',
    help=(
      'Set the machine type within the default cpu node pool. For'
      ' regional clusters, all zones must support the machine type.'
    )
)
cluster_create_optional_arguments.add_argument(
  '--cluster-cpu-machine-type',
    type=str,
    default='',
    help=(
      'Getting deprecated soon! Please use --default-pool-cpu-machine-type'
      'instead, to denote the machine type of the default cpu node pool. Set'
      ' the machine type of other cpu nodepools using --device-type.'
    )
)
cluster_create_optional_arguments.add_argument(
    '--custom-cluster-arguments',
    type=str,
    default='',
    help=(
        'Users can add their own arguments to customize their cluster create'
        ' command. Do note, these will not override already used cluster'
        ' creation arguments.'
        " e.g. --custom-cluster-arguments='--network=mtu9k --subnetwork=mtu9k'"
    ),
)
cluster_create_optional_arguments.add_argument(
    '--custom-tpu-nodepool-arguments',
    type=str,
    default='',
    help=(
        'Users can add their own arguments to customize their tpu node pool'
        ' create command. Do note, these will not override already used node'
        ' pool creation arguments. e.g.'
        ' --custom-tpu-nodepool-arguments="--enable-ip-alias"'
    ),
)
cluster_create_optional_arguments.add_argument(
    '--force',
    action='store_true',
    help=(
      'Forces node pool creation and delete commands to run without additional'
      ' approval.'
    ),
)
add_shared_arguments(cluster_create_optional_arguments)

cluster_create_parser.set_defaults(func=cluster_create)

### "cluster delete" command parser ###
cluster_delete_parser = cluster_subcommands.add_parser(
    'delete',
    help='Delete cloud clusters.',
)
cluster_delete_required_arguments = cluster_delete_parser.add_argument_group(
    'Required Arguments',
    'Arguments required for cluster delete.',
)
cluster_delete_optional_arguments = cluster_delete_parser.add_argument_group(
    'Optional Arguments', 'Arguments optional for cluster delete.'
)

### Required arguments
cluster_delete_required_arguments.add_argument(
    '--cluster',
    type=str,
    default=None,
    help='The name of the cluster to be deleted.',
    required=True,
)

### Optional Arguments
add_shared_arguments(cluster_delete_optional_arguments)
cluster_delete_parser.set_defaults(func=cluster_delete)

### "cluster cacheimage" command parser ###
cluster_cacheimage_parser = cluster_subcommands.add_parser(
    'cacheimage',
    help='Cache image.',
)
cluster_cacheimage_required_arguments = (
    cluster_cacheimage_parser.add_argument_group(
        'Required Arguments',
        'Arguments required for cluster cacheimage.',
    )
)
cluster_cacheimage_optional_arguments = (
    cluster_cacheimage_parser.add_argument_group(
        'Optional Arguments', 'Arguments optional for cluster cacheimage.'
    )
)
cluster_cacheimage_group = cluster_cacheimage_parser.add_mutually_exclusive_group(required=True)

### Device Type Argument
cluster_cacheimage_group.add_argument(
    '--tpu-type',
    type=str,
    default=None,
    help='The tpu type to cache images on, v5litepod-16, etc.'
)
cluster_cacheimage_group.add_argument(
    '--device-type',
    type=str,
    default=None,
    help='The device type to cache images on (can be tpu or gpu), v5litepod-16, h100-80gb-8, etc.'
)

### Required arguments
cluster_cacheimage_required_arguments.add_argument(
    '--cluster',
    type=str,
    default=None,
    help='The name of the cluster to cache the image.',
    required=True,
)
cluster_cacheimage_required_arguments.add_argument(
    '--docker-image',
    type=str,
    default=None,
    help='The docker-image to cache.',
    required=True,
)

### Optional Arguments
add_shared_arguments(cluster_cacheimage_optional_arguments)
cluster_cacheimage_optional_arguments.add_argument(
    '--cache-key',
    type=str,
    default='containerimage',
    help='The key to cache the docker image under.',
    required=False,
)
cluster_cacheimage_parser.set_defaults(func=cluster_cacheimage)

### "cluster describe" command parser ###
cluster_describe_parser = cluster_subcommands.add_parser(
    'describe',
    help='Describe a cluster.',
)
cluster_describe_required_arguments = (
    cluster_describe_parser.add_argument_group(
        'Required Arguments',
        'Arguments required for cluster describe.',
    )
)
cluster_describe_optional_arguments = (
    cluster_describe_parser.add_argument_group(
        'Optional Arguments', 'Arguments optional for cluster describe.'
    )
)

### Required arguments
cluster_describe_required_arguments.add_argument(
    '--cluster',
    type=str,
    default=None,
    help='The name of the cluster to be describe.',
    required=True,
)
### Optional Arguments
add_shared_arguments(cluster_describe_optional_arguments)


cluster_describe_parser.set_defaults(func=cluster_describe)

# "cluster list" command parser.
cluster_list_parser = cluster_subcommands.add_parser(
    'list', help='List cloud clusters.'
)
cluster_list_optional_arguments = cluster_list_parser.add_argument_group(
    'Optional Arguments', 'Arguments optional for cluster list.'
)
### Optional Arguments
add_shared_arguments(cluster_list_optional_arguments)


cluster_list_parser.set_defaults(func=cluster_list)

#### "workload" command parser. ####
workload_parser = xpk_subcommands.add_parser(
    'workload', help='commands around workload management'
)

workload_parser.set_defaults(func=default_subcommand_function)
workload_subcommands = workload_parser.add_subparsers(
    title='workload subcommands',
    dest='xpk_workload_subcommands',
    help='`create`, `list` and `delete` workloads on clusters',
)

# "workload create" command parser.
workload_create_parser = workload_subcommands.add_parser(
    'create', help='Create a new job.'
)
workload_create_parser_required_arguments = (
    workload_create_parser.add_argument_group(
        'Workload Built-in Arguments',
        'Configure xpk to create a Workload for you.'
    )
)
workload_create_parser_optional_arguments = (
    workload_create_parser.add_argument_group(
        'Optional Arguments', 'Arguments optional for `job create`.'
    )
)
workload_base_docker_image_arguments = (
    workload_create_parser.add_argument_group(
        'Base Docker Image Arguments',
        'User supplies a base image or by default the image is set by xpk.'
        ' Xpk will add the `script_dir` to the base image creating an anonymous'
        ' docker image. These arguments are exclusive to `--docker-image`.'
    )
)
workload_docker_image_arguments = (
    workload_create_parser.add_argument_group(
        'Docker Image Arguments',
        '`--base-docker-image` is used by default. Set this argument if the'
        ' user wants the docker image to be used directly by the xpk workload.'
    )
)


### Workload required arguments
workload_create_parser_required_arguments.add_argument(
    '--workload',
    type=workload_name_type,
    default=None,
    help='The name of the workload to run.',
    required=True,
)
workload_create_parser_required_arguments.add_argument(
    '--command',
    type=str,
    default=None,
    help=(
        'Main command to run on each VM. This script runs within the docker '
        'container. Typically this looks like "--command=\'python3 train.py\'" '
        'but if your docker container is missing the dependencies, it might '
        'look more like "--command=\'bash setup.sh && python3 train.py\'".'
    ),
    required=True,
)
workload_create_parser_required_arguments.add_argument(
    '--cluster',
    type=str,
    default=None,
    help='The name of the cluster to run the job on.',
    required=True,
)

workload_device_group = workload_create_parser_required_arguments.add_mutually_exclusive_group(required=True)

workload_device_group.add_argument(
    '--tpu-type',
    type=str,
    default=None,
    help='The tpu type to use, v5litepod-16, etc.'
)
workload_device_group.add_argument(
    '--device-type',
    type=str,
    default=None,
    help='The device type to use (can be tpu or gpu or cpu), v5litepod-16, h100-80gb-8, n2-standard-32-4 etc.'
)

### Workload Optional Arguments
add_shared_arguments(workload_create_parser_optional_arguments)

workload_create_parser_optional_arguments.add_argument(
    '--docker-name',
    type=str,
    default='jax-tpu',
    help=(
        'The name of the docker-image to use, default and typically `jax-tpu`.'
    ),
)
workload_docker_image_arguments.add_argument(
    '--docker-image',
    type=str,
    help=(
        'The version of the docker-image to use. By default, '
        ' `--base-docker-image` is used. Set this argument if the user wants'
        ' the docker image to be used directly by the xpk workload.'
        ' a custom docker image it is typically addressed as'
        ' gcr.io/${PROJECT}/${NAME}:latest. This docker image will be used'
        ' directly by the xpk workload.'
    ),
)
workload_base_docker_image_arguments.add_argument(
    '--base-docker-image',
    type=str,
    default=default_docker_image,
    help=(
        f'The base docker-image to use, default {default_docker_image}. If'
        ' using a custom docker image it is typically addressed as'
        ' gcr.io/${PROJECT}/${NAME}:latest. This docker image will be used as a'
        ' base image by default and the `--script-dir` by default'
        ' will be added to the image.'
    ),
)
workload_base_docker_image_arguments.add_argument(
    '--script-dir',
     type=directory_path_type,
     default=default_script_dir,
    help='The local location of the directory to copy to the docker image and'
        ' run the main command from. Defaults to current working directory.'
)
workload_create_parser_optional_arguments.add_argument(
    '--num-slices',
    type=str,
    default=1,
    help='The number of slices to use, default=1.',
)
workload_create_parser_optional_arguments.add_argument(
    '--env-file',
    type=str,
    default=None,
    help=(
        'Environment file to be applied to the container.  This file should '
        'use the syntax <variable>=value (which sets the variable to the given '
        'value) or <variable> (which takes the value from the local '
        'environment), and # for comments.'
    ),
)
workload_create_parser_optional_arguments.add_argument(
    '--priority',
    type=str,
    default='medium',
    choices=['very-low', 'low', 'medium', 'high', 'very-high'],
    help=(
        'A priority, one of `very-low`, `low`, `medium`, `high` or `very-high`.'
        ' Defaults to `medium`.'
    ),
)
workload_create_parser_optional_arguments.add_argument(
    '--scheduler',
    type=str,
    default='default-scheduler',
    help=(
        'Which scheduler you want to use. Defaults to `default-scheduler`.'
        'If your cluster is configured for high throughput scheduling you might'
        'want to use `gke.io/high-throughput-scheduler`.'
    ),
)
workload_create_parser_optional_arguments.add_argument(
    '--max-restarts',
    type=str,
    default='0',
    help=(
        'Maximum number of times the JobSet will be restarted upon failure. '
        'Defaults to 0.'
    ),
)
workload_create_parser_optional_arguments.add_argument(
    '--debug-dump-gcs',
    type=str,
    default=None,
    help=(
        'GCS bucket or a directory within a bucket, e.g gs://bucket/subdir, '
        'where debugging information such as HLO dumps are uploaded'
    ),
)

workload_create_parser_optional_arguments.add_argument(
    '--deploy-stacktrace-sidecar',
    action='store_true',
    help=(
        'Add this argument to deploy a sidecar container that will '
        'read the stack traces collected in /tmp/debugging directory '
        'and forward them to Cloud Logging for TPU workloads.'
    ),
)

workload_create_parser_optional_arguments.add_argument(
    '-tgps', '--termination-grace-period-seconds', 
    type=str,
    default='30',
    help=(
        'Maximum wait time for a workload Pod to wrap up after a disruption event or deletion request.'
        'Defaults to 30 seconds.'
    ),
)

workload_create_parser.set_defaults(func=workload_create)

# "job delete" command parser.
workload_delete_parser = workload_subcommands.add_parser(
    'delete', help='Delete job.'
)
workload_delete_parser_required_arguments = (
    workload_delete_parser.add_argument_group(
        'Required Arguments',
        'Arguments required for `job delete`.',
    )
)
workload_delete_parser_optional_arguments = (
    workload_delete_parser.add_argument_group(
        'Optional Arguments', 'Arguments optional for `job delete`.'
    )
)
add_shared_arguments(workload_delete_parser_optional_arguments)

### Required arguments
workload_delete_parser_required_arguments.add_argument(
    '--cluster',
    type=str,
    default=None,
    help='The name of the cluster to delete the job on.',
    required=True,
)

### Optional arguments
workload_delete_parser_optional_arguments.add_argument(
    '--workload',
    type=workload_name_type,
    default=None,
    help='The name of the workload to delete. If the workload is not specified, '
    'all workloads will be deleted from the cluster.',
)
workload_delete_parser_optional_arguments.add_argument(
    '--filter-by-job',
    type=str,
    help='Filters the arguments based on job name. Provide a regex expression'
          'to parse jobs that match the pattern or provide a job name to delete a single job.',
)
workload_delete_parser_optional_arguments.add_argument(
    '--filter-by-status',
    type=str,
    default='EVERYTHING',
    choices=['EVERYTHING', 'FINISHED', 'RUNNING', 'QUEUED', 'FAILED', 'SUCCESSFUL'],
    help='Filters the arguments based on status. Selected filters are listed'
        ' above. FAILED and SUCCESSFUL are sub-states of FINISHED.',
    required=False,
)
workload_delete_parser_optional_arguments.add_argument(
    '--force',
    action='store_true',
    help=(
      'Forces workload deletion command to run without additional approval.'
    ),
)

workload_delete_parser.set_defaults(func=workload_delete)

# "workload list" command parser.
workload_list_parser = workload_subcommands.add_parser(
    'list', help='List jobs.'
)

workload_list_parser.add_argument(
    '--cluster',
    type=str,
    default=None,
    help='The name of the cluster to list jobs on.',
    required=True,
)

workload_list_parser.add_argument(
    '--filter-by-status',
    type=str,
    default='EVERYTHING',
    choices=['EVERYTHING', 'FINISHED', 'RUNNING', 'QUEUED', 'FAILED', 'SUCCESSFUL'],
    help='Filters the arguments based on status. Selected filters are listed'
        ' above. FAILED and SUCCESSFUL are sub-states of FINISHED.',
    required=False,
)

workload_list_parser.add_argument(
    '--filter-by-job',
    type=str,
    help='Filters the arguments based on job name. Provide a regex expression'
          'to parse jobs that match the pattern or provide a job name to view a single job.',
    required=False,
)
add_shared_arguments(workload_list_parser)


workload_list_parser.set_defaults(func=workload_list)

xpk_print('Starting xpk', flush=True)
main_args = parser.parse_args()
main_args.func(main_args)


################### Main ###################
def main() -> None:
  xpk_print('XPK Done.', flush=True)


if __name__ == '__main__':
  main()

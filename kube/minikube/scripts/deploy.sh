#!/bin/bash

# Function to check if a resource exists
check_resource_exist() {
  local kind=$1
  local name=$2

  # Wait for the resource to be available
  while [[ -z "$(kubectl get $kind $name -o json)" ]]; do
    echo "Waiting for $kind $name to be available..."
    sleep 5
  done

  echo "$kind $name is available!"
}

# Function to check if a secret 'kci-api-token' exists
check_secret_key_exists() {
  local kind=$1
  local name=$2

  # Wait for the resource to be available
  while [[ -z "$(kubectl get $kind $name -o jsonpath='{.data.*}')" ]]; do
    echo "Waiting for $kind $name to be available..."
    sleep 5
  done

  echo "$kind $name is available!"
}

# Fork GitHub repository in Minikube node
kubectl apply -f ../init/init-job.yaml
kubectl wait --for=condition=Complete --timeout=60s job/pipeline-github-cloning-job
exit_code=$?
if [ $exit_code -ne 0 ];
then
   echo "Error: Init job could not complete"
   exit 1
else
   echo "Init job is completed successfully"
fi

# Create Kubernetes secret
kubectl create secret generic kernelci-pipeline-secrets --from-literal=kci-api-token=$KCI_API_TOKEN
check_secret_key_exists secret kernelci-pipeline-secrets -o jsonpath='{.data.*}'

# Generate configmap
kubectl create -f ../configmap/pipeline-configmap.yaml
check_resource_exist "configmaps" "kernelci-pipeline-config"

# Apply all the deployments
for file in ../deployments/*.yaml; do
  kubectl apply -f "$file"
  name=$(kubectl get -f "$file" -o 'jsonpath={.metadata.name}')
  kubectl wait --for=condition=Available --timeout=60s deploy "$name"
done

echo "All components deployed successfully!"

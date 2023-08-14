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

# Function to check if a resource is ready (for Deployments)
check_deployment_ready() {
  local name=$1

  # Wait for the Deployment to have available replicas
  while [[ "$(kubectl get deployment $name -o 'jsonpath={.status.availableReplicas}')" != "$(kubectl get deployment $name -o 'jsonpath={.status.replicas}')" ]]; do
    echo "Waiting for Deployment $name to have available replicas..."
    sleep 5
  done

  echo "Deployment $name is ready!"
}

# Function to check if a resource is completed (for Jobs)
check_job_completed() {
  local name=$1

  # Wait for the Job to be completed
  while [[ "$(kubectl get job $name -o 'jsonpath={.status.conditions[?(@.type=="Complete")].status}')" != "True" ]]; do
    echo "Waiting for Job $name to be completed..."
    sleep 5
  done

  echo "Job $name is completed!"
}

# Function to apply a resource and check if it is ready or completed (for Jobs)
apply_and_check_resource() {
  local file=$1

  kubectl apply -f "$file"

  # Get the resource kind and name from the file
  kind=$(kubectl get -f "$file" -o 'jsonpath={.kind}')
  name=$(kubectl get -f "$file" -o 'jsonpath={.metadata.name}')

  case "$kind" in
    "Job")
      check_job_completed "$name"
      ;;
    "Deployment")
      check_deployment_ready "$name"
      ;;
    *)
      ;;
  esac
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
kubectl apply -f ../init/init-pod.yaml
check_job_completed "github-cloning-pod"

# Create Kubernetes secret
kubectl create secret generic kernelci-pipeline-secrets --from-literal=kci-api-token=$KCI_API_TOKEN
check_secret_key_exists secret kernelci-pipeline-secrets -o jsonpath='{.data.*}'

# Generate configmap
kubectl create -f ../configmap/pipeline-configmap.yaml
check_resource_exist "configmaps" "kernelci-pipeline-config"

# Apply all the deployments
for file in ../deployments/*.yaml; do
  apply_and_check_resource "$file"
done

echo "All components deployed successfully!"

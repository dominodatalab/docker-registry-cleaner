#!/usr/bin/env bash

if [[ $# -eq 0 ]];then
  PASSWORD=$SKOPEO_PASSWORD
else
  PASSWORD=$1
fi

# GNU Sed and MacOS Sed have different options
# Detect when running on Mac and adjust accordingly
if [[ "$OSTYPE" == "darwin"* ]]; then
  SEDOPTION='-i \x27\x27'
else
  SEDOPTION='-i'
fi


echo "Creating Skopeo pod"
kubectl apply -f pod.yaml
pod_name=$(cat pod.yaml | grep name: | head -1 | cut -d ":" -f 2 | tr -d '[:space:]')
pod_namespace=$(cat pod.yaml | grep namespace: | head -1 | cut -d ":" -f 2 | tr -d '[:space:]')
kubectl wait --for=condition=Ready pod/$pod_name -n $pod_namespace


# Make an environments file for a list of all the environment IDs to remove, followed by any revisions to retain
# eg "62798b9bee0eb12322fc97e8 31 30"
if [[ ! -f environments ]] || [[ `wc -l environments | awk '{print $1}'` -eq "0" ]]; then
  echo "Could not find valid environments file"
  exit 1
fi

if [[ -f revisions_to_delete ]]; then
  rm revisions_to_delete
fi

# Make a folder structure to store all the tags for all of the images.
# This will make it easier to check what was moved.
mkdir -p tags/done

# Get all Docker tags for /dominodatalab/environment repository
kubectl exec -it -n domino-platform skopeo -- skopeo list-tags --tls-verify=false --creds domino-registry:$PASSWORD docker://docker-registry:5000/dominodatalab/environment | jq -r '.Tags[]' > ./dominodatalab_environment_tags

# Read repos from the environments file
while read environment; do
  # Turn each line of environments file into an array
  IFS=' ' read -ra array <<<$environment
  # Ignore comments on environment IDs
  if [[ ${array[0]} = "\#*" ]]; then
    environmentId="${${array[0]}:1}"
  else
    environmentId=${array[0]}
  fi
  echo "Checking /dominodatalab/environment:$environmentId-*"
  # Find all revisions of this environment and mark them for deletion
  cat dominodatalab_environment_tags | grep ${array[0]} >> revisions_to_delete
  i=1
  while [[ $i -lt ${#array[@]} ]]; do
    while read tag; do
      # Check if current tag matches an environment revision to keep
      # If so, remove it from revisions_to_delete
      if [[ $tag == "$environmentId-${array[$i]}" ]]; then
        echo "Found tag $tag in Environments file"
        sed $SEDOPTION -e "/^${tag}$/d" revisions_to_delete
      fi
    done < revisions_to_delete
    ((i++))
  done
done < ./environments

# Read repos from the environments file
while read environment; do
  # Turn each line of environments file into an array
  IFS=' ' read -ra array <<<$environment
  ## This skopeo command is buggy– the script will fail if you try to list tags on a repository that doesn't exist
  ## Comment out the name of any environment names you would like to skip
  if [[ ${array[0]} = "\#*" ]]; then
    continue
  else
    environmentId=${array[0]}
    echo "Checking domino-$environmentId"
    kubectl exec -it -n domino-platform skopeo -- skopeo list-tags --tls-verify=false --creds domino-registry:$PASSWORD docker://docker-registry:5000/domino-$environmentId | jq -r '.Tags[]' > ./tags/domino-$environmentId
  fi
  if [[ -f ./tags/domino-$environmentId ]] && [[ `wc -l ./tags/domino-$environmentId | awk '{print $1}'` -ge "1" ]]; then
    i=1
    while [[ $i -lt ${#array[@]} ]]; do
      while read tag; do
        # Check if current tag matches an environment revision to keep
        # If so, remove it from ./tags/<environment> file
        if [[ $tag == ${array[$i]} ]]; then
          echo "$tag matches a revision to retain– removing from file"
          sed $SEDOPTION -e "/^${tag}$/d" ./tags/domino-$environmentId
        fi
      done < ./tags/domino-$environmentId
      ((i++))
    done
    sed $SEDOPTION -e "/^buildcache$/d" ./tags/domino-$environmentId
  fi
done < ./environments

# For security, deletion of Docker images is disabled in Domino's Docker registry by default
echo "Enabling deletion of Docker images"
kubectl set env -n domino-platform sts/docker-registry REGISTRY_STORAGE_DELETE_ENABLED=true
kubectl wait --for=condition=Ready -n domino-platform pod/docker-registry-0


# Delete revisions from /dominodatalab/environment
while read revision_to_delete; do
  echo "Deleting docker-registry:5000/dominodatalab/environment:${revision_to_delete}"
#  kubectl exec -i -n domino-platform skopeo -- skopeo delete --tls-verify=false --creds domino-registry:$PASSWORD docker://docker-registry:5000/dominodatalab/environment:${revision_to_delete}
done < revisions_to_delete

# Delete revisions from /domino-<environmentId>
while read environment; do
  IFS=' ' read -ra array <<<$environment
  environmentId=${array[0]}
  if [[ -f ./tags/domino-$environmentId ]] && [[ `wc -l ./tags/domino-$environmentId | awk '{print $1}'` -ge "1" ]]; then
    while read revision_to_delete; do
      echo "Deleting docker-registry:5000/domino-$environmentId:${revision_to_delete}"
#      kubectl exec -i -n domino-platform skopeo -- skopeo delete --tls-verify=false --creds domino-registry:$PASSWORD docker://docker-registry:5000/domino-$environmentId:${revision_to_delete}
    done < ./tags/domino-$environmentId
    mv ./tags/domino-$environmentId  ./tags/done/domino-$environmentId
  fi
done < ./environments


echo "Disabling deletion of Docker images"
kubectl set env -n domino-platform sts/docker-registry REGISTRY_STORAGE_DELETE_ENABLED-
kubectl wait --for=condition=Ready -n domino-platform pod/docker-registry-0

echo "Deleting Skopeo pod"
kubectl delete -f pod.yaml

echo "ALL DONE!"

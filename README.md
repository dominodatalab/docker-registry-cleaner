# docker-registry-cleaner
Script to remove unneeded images from Docker.

This project was created to automate the deletion of unused Compute Environments in Domino's Docker registry using Skopeo, and builds upon an existing script that was written to migrate all images from one registry to another.

Domino stores Compute Environments in Docker with two naming conventions:
`<server>:5000/domino-<computeEnvironmentId>:<revision>`
is an older format and
`<server>5000/dominodatalab/environment:<computeEnvironmentId>-<revision>`
is found in newer Domino instances. 
If a Domino instance has upgraded over time, older environment revisions may have the first format, while newer revisions may have the second.

Because these naming conventions differ (in one, the tag is just the revision number, while in the other, the tag is the Compute Environment's ID and the revision number), we need two different methods to list and delete the tags for any one environment.

If you know your environment images are all in one repository (on a newer instance, or one that never adopted the newer naming convention), consider commenting out the sections of code that relate to the other naming convention, for speed and reliability.


# Usage
The script needs three things to run:
* Kubectl access to the cluster where you would like to delete images. The script will use your current Kubectl context.

* An `environments` file with the IDs of the Compute Environments to delete, followed by any revisions to retain, separated by spaces.
`environments-example` contains an example of the required format. Rename it and modify it to fit your needs.
```
62798b9bee0eb12322fc97e8 31 30
6286a3c76d4fd0362f8ba3ec 13 12 9
627d94043035a63be6140e93 10
```

* The password for the Docker registry (which can be obtained from the `domino-registry` secret in the domino-compute namespace).
This can either be exported as the environment variable `SKOPEO_PASSWORD`:  
```
export SKOPEO_PASSWORD=GewXRrP0XoNxn07B2lwH4UnvMQeG83Rk
```
or passed in as a positional argument when running the script:
```
./docker_delete.sh GewXRrP0XoNxn07B2lwH4UnvMQeG83Rk
```


# How it Works
* The script deploys a Skopeo pod into the `domino-platform` namespace with the labels required to allow it through the docker-registry network policy.
* The script checks for the required local file structure

For `dominodatalab/environment`:
* Queries Docker for a list of tags in the `dominodatalab/environment` repository.
* Checks the list for any tags that match the list of environment IDs in the `environments` file.
* Writes all of those tags – minus the ones that should be retained – to `revisions_to_delete`.

For `domino-<environmentId>`:
* Queries Docker for a list of tags in the `domino-<environmentId>` repository and writes them to `./tags/domino-<environmentId>`.
* Removes any tags from `./tags/domino-<environmentId>` that should be retained.

Then:
* The script enables deletion of Docker images (disabled by default, for security).
* The script iterates over `revisions_to_delete` and deletes each tag from `dominodatalab/environment`.
* For each environment in `environments`, the script iterates over `./tags/domino-<environmentId>` and deletes each tag from `domino-<environmentId>`.
* When the script has finished all the tags in `./tags/domino-<environmentId>`, it moves the file to `./tags/done`.
* The script disables deletion of Docker images again.
* The script deletes the Skopeo pod.

### Note:
The `skopeo delete` commands are intentionally commented out to allow you to check the contents of `revisions_to_delete` and `./tags/domino-<environmentId>` before committing to deleting anything.
Once you are happy to go, uncomment these lines.

# Known Issues
* If you run `skopeo list-tags` against a Docker repository that doesn't exist, it will crash the script.
This can cause issues listing tags for `domino-<environmentId>`, so a check was added to allow commenting out environment IDs in `environments`.
and code relating to `domino-<environmentId>` will skip any environments that are commented out.

* If you have no environment revisions in `dominodatalab/environment`, or the repository doesn't exist at all, comment out the two code blocks that list and delete tags in it.

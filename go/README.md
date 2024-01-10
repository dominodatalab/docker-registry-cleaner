# Docker Layer Analyzer

**Warning:** This is a sample program of a proof-of-concept type not intended for any productional use. 

This utility fetches image information from a set of Docker repositories and reports the size of their constituent 
layers as well as a frequency of their use.

## How to use

* Make sure that both `skopeo` and `go` are installed.

* After cloning the repo, compile the go program:

```
cd go
go build .
```

* Login to the Docker **in skopeo**:

```
aws ecr get-login-password --region us-west-2 | skopeo login \
    --username AWS --password-stdin \
    946429944765.dkr.ecr.us-west-2.amazonaws.com
```

* Run the program:

```
./docker-registry-cleaner layers|images <docker-registry-address> <domino-environment>
```

* **layers** - Will list all the layers (sorted by use and size) and image tags for each layer.
* **images** - Will list all the images and layers information for each image. 

The output will be formatted as json. Redirect stderr to `/dev/null` to hide logging.

For example, these are valid arguments:
```
./docker-registry-cleaner layers 946429944765.dkr.ecr.us-west-2.amazonaws.com stevel33582 2>/dev/null
```

(It's not really a "cleaner", the name is confusing. The program will run for a minute and then
print some useful information on stdout.)
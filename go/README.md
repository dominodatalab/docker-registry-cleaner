# Docker Layer Analyzer

**Warning:** This is a sample program of a _proof-of-concept_ type not intended for any productional use. 

This utility fetches image information from a set of Docker repositories and reports the size of their constituent 
layers as well as a frequency of their use.

The Domino environment is currently hardcoded to `stevel33582`.

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

(You may also need to login to docker using the same method; simply replace `skopeo` to `docker` )

* Run the program:

```
    ./docker-registry-cleaner
```

It's not really a "cleaner", the name is confusing. The program will run for a minute and then
print some useful information on stdout.
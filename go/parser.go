package main

import (
	"encoding/json"
	"fmt"
	"log"
	"os/exec"
	"time"
)

const dockerHost = "946429944765.dkr.ecr.us-west-2.amazonaws.com"

type ImageMeta struct {
	Name          string            `json:"Name"`
	Digest        string            `json:"Digest"`
	RepoTags      []string          `json:"RepoTags"`
	Created       time.Time         `json:"Created"`
	DockerVersion string            `json:"DockerVersion"`
	Labels        map[string]string `json:"Labels"`
	Architecture  string            `json:"Architecture"`
	Os            string            `json:"Os"`
	Layers        []string          `json:"Layers"`
	LayersData    []struct {
		MIMEType    string      `json:"MIMEType"`
		Digest      string      `json:"Digest"`
		Size        int         `json:"Size"`
		Annotations interface{} `json:"Annotations"`
	} `json:"LayersData"`
	Env []string `json:"Env"`
}

func readTags(envName string, repoName string) ([]string, error) {
	registry := fmt.Sprintf("docker://%s/%s/%s", dockerHost, envName, repoName)
	cmd := exec.Command("skopeo", "list-tags", registry)
	log.Printf("Executing '%s'\n", cmd)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	type tagList struct {
		Repository string
		Tags       []string
	}
	var tags tagList
	if err := json.NewDecoder(stdout).Decode(&tags); err != nil {
		return nil, err
	}
	if err := cmd.Wait(); err != nil {
		return nil, err
	}
	return tags.Tags, nil
}

func readImageMetadata(envName string, repoName string, tag string) (ImageMeta, error) {
	registry := fmt.Sprintf("docker://%s/%s/%s:%s", dockerHost, envName, repoName, tag)
	cmd := exec.Command("skopeo", "inspect", registry)
	log.Printf("Executing '%s'\n", cmd)
	var imageMeta ImageMeta
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return imageMeta, err
	}
	if err := cmd.Start(); err != nil {
		return imageMeta, err
	}
	//buf, err := io.ReadAll(stdout)
	//if err != nil {
	//	return "", err
	//}
	//return string(buf), nil
	if err := json.NewDecoder(stdout).Decode(&imageMeta); err != nil {
		return imageMeta, err
	}
	if err := cmd.Wait(); err != nil {
		return imageMeta, err
	}
	return imageMeta, nil
}

package main

import (
	"encoding/json"
	"fmt"
	"log"
	"os/exec"
)

func readTags(repoName string) ([]string, error) {
	registry := fmt.Sprintf("docker://%s/%s/%s", dockerAddress, envName, repoName)
	cmd := exec.Command("skopeo", "list-tags", registry)
	log.Printf("Executing '%s'\n", cmd)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	var tagList struct {
		Repository string   `json:"Repository"`
		Tags       []string `json:"Tags"`
	}
	if err := json.NewDecoder(stdout).Decode(&tagList); err != nil {
		return nil, err
	}
	if err := cmd.Wait(); err != nil {
		return nil, err
	}
	return tagList.Tags, nil
}

func readImageData(repoName string, tag string) (SkopeoImageData, error) {
	registry := fmt.Sprintf("docker://%s/%s/%s:%s", dockerAddress, envName, repoName, tag)
	cmd := exec.Command("skopeo", "inspect", registry)
	log.Printf("Executing '%s'\n", cmd)
	var image SkopeoImageData
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return image, err
	}
	if err := cmd.Start(); err != nil {
		return image, err
	}
	if err := json.NewDecoder(stdout).Decode(&image); err != nil {
		return image, err
	}
	if err := cmd.Wait(); err != nil {
		return image, err
	}
	return image, nil
}

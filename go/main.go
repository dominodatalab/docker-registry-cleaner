package main

import (
	"fmt"
	"log"
	"sort"
	"time"
)

const envName = "stevel33582"

var repoNames = []string{"environment", "model"}

type ImageData struct {
	Name          string            `json:"Name"`
	Digest        string            `json:"Digest"`
	RepoTags      []string          `json:"RepoTags"`
	Created       time.Time         `json:"Created"`
	DockerVersion string            `json:"DockerVersion"`
	Labels        map[string]string `json:"Labels"`
	Architecture  string            `json:"Architecture"`
	Os            string            `json:"Os"`
	Layers        []string          `json:"Layers"`
	LayersData    []LayerData       `json:"LayersData"`
	Env           []string          `json:"Env"`
}

type LayerData struct {
	MIMEType    string      `json:"MIMEType"`
	Digest      string      `json:"Digest"`
	Size        int         `json:"Size"`
	Annotations interface{} `json:"Annotations"`
}

// tag -> image info
var allImages map[string]ImageData = make(map[string]ImageData)

// digest -> layer info
var allLayers map[string]LayerData = make(map[string]LayerData)

// layer digest -> image tag[]
var layersUse map[string][]string = make(map[string][]string)

func main() {
	processEnvironment(envName)

	allLayerDigests := make([]string, 0, len(allLayers))
	for digest := range allLayers {
		allLayerDigests = append(allLayerDigests, digest)
	}
	fmt.Println("\n****** SUMMARY ACROSS ALL IMAGES IN ALL REPOSITORIES")
	printLayerSummary(allLayerDigests)

	for tag := range allImages {
		printImageSummary(tag)
	}
}

func printLayerSummary(layers []string) {
	type LayerUseSummary struct {
		digest    string
		frequency int
		size      int
	}
	var layerUseSummary []LayerUseSummary
	for _, digest := range layers {
		layerUseSummary = append(layerUseSummary, LayerUseSummary{digest: digest, size: allLayers[digest].Size, frequency: len(layersUse[digest])})
	}
	sort.Slice(layerUseSummary,
		func(i, j int) bool {
			if layerUseSummary[i].frequency == layerUseSummary[j].frequency {
				return layerUseSummary[i].size > layerUseSummary[j].size
			} else {
				return layerUseSummary[i].frequency > layerUseSummary[j].frequency
			}
		})
	fmt.Printf("%-71s %10s %5s\n", "DIGEST", "SIZE", "FREQ")
	for _, summary := range layerUseSummary {
		fmt.Printf("%71s %10d %5d\n", summary.digest, summary.size, summary.frequency)
	}
}

func printImageSummary(tag string) {
	fmt.Printf("\n****** IMAGE: %s\n", tag)
	printLayerSummary(allImages[tag].Layers)
}

func processEnvironment(envName string) {
	for _, repoName := range repoNames {
		processRepo(envName, repoName)
	}
}

func processRepo(envName string, repoName string) {
	tags, err := readTags(envName, repoName)
	if err != nil {
		log.Fatal(err)
	}
	for _, tag := range tags {
		processImage(envName, repoName, tag)
	}
}

func processImage(envName string, repoName string, tag string) {
	image, err := readImageData(envName, repoName, tag)
	if err != nil {
		log.Fatal(err)
	}
	allImages[tag] = image
	for _, layer := range image.LayersData {
		digest := layer.Digest
		_, ok := allLayers[digest]
		if !ok {
			allLayers[digest] = layer
		}
		_, ok = layersUse[digest]
		if !ok {
			layersUse[digest] = []string{tag}
		} else {
			layersUse[digest] = append(layersUse[digest], tag)
		}
	}

}

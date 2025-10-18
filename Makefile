CONFIG_FILE ?= make.env

REGISTRY ?=
IMAGE_NAME ?= coinbase-local
TAG ?=
PLATFORM ?= linux/amd64

ifneq ("$(wildcard $(CONFIG_FILE))","")
  include $(CONFIG_FILE)
endif

GIT_SHA := $(shell git rev-parse --short HEAD 2>/dev/null)
IMAGE_TAG := $(if $(TAG),$(TAG),$(if $(GIT_SHA),$(GIT_SHA),latest))
IMAGE_REPO := $(if $(REGISTRY),$(REGISTRY)/,)$(IMAGE_NAME)
IMAGE := $(IMAGE_REPO):$(IMAGE_TAG)

.PHONY: build push image-name help

build: ## Build the application image
	docker build --platform $(PLATFORM) -t $(IMAGE) .

push: build ## Push the application image
	docker push $(IMAGE)

image-name: ## Print the image reference that will be used
	@echo $(IMAGE)

help: ## Show available make targets
	@grep -E '^[a-zA-Z_-]+:.*?## .+' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "%-12s %s\n", $$1, $$2}'

# From zero to a run on a local kind cluster:
#   make kind-up && make deploy && make run
# Needs docker, kind and kubectl. Reads .env at the repo root.
IMAGE    ?= ankigen:local
CLUSTER  ?= ankigen
PIPELINE ?= data-platform
NS       := ankigen
ROOT     := ../..

.PHONY: kind-up deploy run logs kind-down

kind-up:
	kind create cluster --config kind-config.yaml
	docker build -t $(IMAGE) $(ROOT)
	kind load docker-image $(IMAGE) --name $(CLUSTER)

# The Secret and the ConfigMap are created here, from files on this machine,
# so neither lives in the repo. --dry-run | apply makes a rerun an update.
deploy:
	test -f $(ROOT)/.env || { echo "no .env at the repo root (copy .env.example)"; exit 1; }
	kubectl apply -f namespace.yaml
	kubectl -n $(NS) create secret generic ankigen-env --from-env-file=$(ROOT)/.env \
		--dry-run=client -o yaml | kubectl apply -f -
	kubectl -n $(NS) create configmap ankigen-profile \
		--from-file=profile.yaml=$(ROOT)/pipelines/$(PIPELINE)/profile.yaml \
		--dry-run=client -o yaml | kubectl apply -f -
	kubectl apply -f pvc.yaml -f cronjob.yaml

# A one-off Job from the CronJob, then follow it.
run:
	kubectl -n $(NS) create job --from=cronjob/ankigen manual-$$(date +%s)
	$(MAKE) logs

logs:
	kubectl -n $(NS) wait --for=condition=Ready pod -l job-name --timeout=300s || true
	kubectl -n $(NS) logs -f -l job-name --all-containers --prefix --tail=-1

kind-down:
	kind delete cluster --name $(CLUSTER)

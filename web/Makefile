# SAM's makefile builder calls build-<LogicalId>. Both functions ship gremlin_web; only the
# worker gets the media and detection libraries (the `worker` dependency group) and the pinned
# detector weights, so the API's package and cold start stay small.
# Python 3.12 uses AL2023; its glibc supports the pinned wheels' manylinux_2_28 builds.
# devtools and tests stay out of both packages.
INSTALL = uv pip install --quiet --target $(ARTIFACTS_DIR) --python-platform aarch64-manylinux_2_28 \
	--python-version 3.12 --only-binary :all: -r $(ARTIFACTS_DIR)/requirements.txt

build-ApiFunction:
	uv export --frozen --no-default-groups --no-hashes --no-emit-project -o $(ARTIFACTS_DIR)/requirements.txt
	$(INSTALL)
	rm $(ARTIFACTS_DIR)/requirements.txt
	cp -R gremlin_web $(ARTIFACTS_DIR)/gremlin_web
	rm -rf $(ARTIFACTS_DIR)/gremlin_web/models $(ARTIFACTS_DIR)/gremlin_web/__pycache__ $(ARTIFACTS_DIR)/gremlin_web/.DS_Store

build-WorkerFunction:
	uv export --frozen --no-default-groups --group worker --no-hashes --no-emit-project -o $(ARTIFACTS_DIR)/requirements.txt
	$(INSTALL)
	rm $(ARTIFACTS_DIR)/requirements.txt
	# Lambda allows 250 MiB unzipped and this package is close (236 MiB on 2 November 2026):
	# drop onnxruntime's C API library (its Python extension links the runtime statically),
	# its training and conversion tools, and every bundled test suite.
	rm -rf $(ARTIFACTS_DIR)/onnxruntime/capi/libonnxruntime.so.* $(ARTIFACTS_DIR)/onnxruntime/transformers \
		$(ARTIFACTS_DIR)/onnxruntime/quantization $(ARTIFACTS_DIR)/onnxruntime/tools $(ARTIFACTS_DIR)/onnxruntime/datasets
	find $(ARTIFACTS_DIR) -type d -name tests -prune -exec rm -rf {} +
	uv run --frozen python -m devtools.models
	cp -R gremlin_web $(ARTIFACTS_DIR)/gremlin_web
	rm -rf $(ARTIFACTS_DIR)/gremlin_web/__pycache__ $(ARTIFACTS_DIR)/gremlin_web/.DS_Store $(ARTIFACTS_DIR)/gremlin_web/models/.gitignore

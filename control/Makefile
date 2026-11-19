# SAM's makefile builder calls build-<LogicalId>; both functions ship the same package.
# Python 3.12 uses AL2023; its glibc supports the pinned decoder's manylinux_2_28 wheels.
build-ApiFunction build-ControllerFunction:
	uv export --frozen --no-dev --no-hashes --no-emit-project -o $(ARTIFACTS_DIR)/requirements.txt
	uv pip install --quiet --target $(ARTIFACTS_DIR) --python-platform aarch64-manylinux_2_28 \
		--python-version 3.12 --only-binary :all: -r $(ARTIFACTS_DIR)/requirements.txt
	rm $(ARTIFACTS_DIR)/requirements.txt
	cp -R gremlin_control $(ARTIFACTS_DIR)/gremlin_control
	rm -rf $(ARTIFACTS_DIR)/gremlin_control/__pycache__

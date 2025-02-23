#!/usr/bin/env bash
#
# Copyright © 2023 Apple Inc. All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

SCRIPT_DIR_PATH="$(
    cd -- "$(dirname "$0")" >/dev/null 2>&1
    pwd -P
)"

EXECUTORCH_ROOT_PATH=$(realpath "$SCRIPT_DIR_PATH/../../../../")
COREML_DIR_PATH="$EXECUTORCH_ROOT_PATH/backends/apple/coreml"
EXAMPLES_COREML_DIR_PATH="$EXECUTORCH_ROOT_PATH/examples/apple/coreml"
IOS_TOOLCHAIN_PATH="$COREML_DIR_PATH/third-party/ios-cmake/ios.toolchain.cmake"
CMAKE_BUILD_DIR_PATH="$EXAMPLES_COREML_DIR_PATH/cmake-out"
LIBRARIES_DIR_PATH="$EXAMPLES_COREML_DIR_PATH/executor_runner/libraries"
INCLUDE_DIR_PATH="$EXAMPLES_COREML_DIR_PATH/executor_runner/include"
EXECUTORCH_INCLUDE_DIR_PATH="$COREML_DIR_PATH/runtime/include/executorch"

cd "$EXECUTORCH_ROOT_PATH"

echo "ExecuTorch: Building executor_runner"

echo "ExecuTorch: Removing build directory $CMAKE_BUILD_DIR"
rm -rf "$CMAKE_BUILD_DIR_PATH"

# Build executorch
echo "ExecuTorch: Building executorch"
cmake "$EXECUTORCH_ROOT_PATH" -B"$CMAKE_BUILD_DIR_PATH" \
-DCMAKE_TOOLCHAIN_FILE="$IOS_TOOLCHAIN_PATH" \
-DPLATFORM=MAC_UNIVERSAL \
-DDEPLOYMENT_TARGET=13.0 \
-DFLATC_EXECUTABLE="$(which flatc)" \
-DEXECUTORCH_BUILD_EXECUTOR_RUNNER=OFF \
-DEXECUTORCH_BUILD_XNNPACK=OFF \
-DEXECUTORCH_BUILD_SDK=ON \
-DEXECUTORCH_BUILD_COREML=ON

cmake --build "$CMAKE_BUILD_DIR_PATH" -j9 -t coremldelegate -t gflags_nothreads_static
cmake --build "$CMAKE_BUILD_DIR_PATH" -j9 -t etdump -t flatccrt

# Copy CoreML delegate headers
echo "ExecuTorch: Copying headers"
echo $EXECUTORCH_INCLUDE_DIR_PATH
rm -rf "$INCLUDE_DIR_PATH"
mkdir "$INCLUDE_DIR_PATH"
#Copy ExecuTorch headers
mkdir -p "$EXECUTORCH_INCLUDE_DIR_PATH"
find extension \( -name "*.h" -o -name "*.hpp" \) -exec rsync -R '{}' "$EXECUTORCH_INCLUDE_DIR_PATH" \;
find runtime \( -name "*.h" -o -name "*.hpp" \) -exec rsync -R '{}' "$EXECUTORCH_INCLUDE_DIR_PATH" \;
find util \( -name "*.h" -o -name "*.hpp" \) -exec rsync -R '{}' "$EXECUTORCH_INCLUDE_DIR_PATH" \;
find sdk \( -name "*.h" -o -name "*.hpp" \) -exec rsync -R '{}' "$EXECUTORCH_INCLUDE_DIR_PATH" \;
cp -rf "$COREML_DIR_PATH/runtime/include/" "$INCLUDE_DIR_PATH"

# Copy required libraries
echo "ExecuTorch: Copying libraries"
mkdir "$LIBRARIES_DIR_PATH"
cp -f "$CMAKE_BUILD_DIR_PATH/libexecutorch.a" "$LIBRARIES_DIR_PATH"
cp -f "$CMAKE_BUILD_DIR_PATH/sdk/libetdump.a" "$LIBRARIES_DIR_PATH"
cp -f "$CMAKE_BUILD_DIR_PATH/backends/apple/coreml/libcoremldelegate.a" "$LIBRARIES_DIR_PATH"
cp -f "$EXECUTORCH_ROOT_PATH/third-party/flatcc/lib/libflatccrt.a" "$LIBRARIES_DIR_PATH"

# Build the runner
echo "ExecuTorch: Building runner"
XCODE_WORKSPACE_DIR_PATH="$EXAMPLES_COREML_DIR_PATH/executor_runner"
XCODE_BUILD_DIR_PATH="$EXAMPLES_COREML_DIR_PATH/xcode-build"

xcodebuild build -workspace "$XCODE_WORKSPACE_DIR_PATH/coreml_executor_runner.xcworkspace" -scheme coreml_executor_runner BUILD_DIR="$XCODE_BUILD_DIR_PATH"
mv -f "$XCODE_BUILD_DIR_PATH/DEBUG/coreml_executor_runner" "$PWD"

#!/bin/bash

cd $SRC/FreeRDP

if [ -d build ]; then
    cd build
    ctest --output-on-failure || exit 1
else
    echo "No build directory found, skipping tests"
    exit 0
fi

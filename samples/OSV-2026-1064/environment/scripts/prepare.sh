#!/bin/bash

cd $SRC/FreeRDP
git submodule update --init --recursive 2>/dev/null || true

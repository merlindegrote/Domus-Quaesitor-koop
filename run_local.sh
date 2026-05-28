#!/bin/bash

# Get the absolute path of the directory containing this script
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR" || exit

# Activate virtual environment
if [ -d "venv" ]; then
    source venv/bin/activate
fi

# Run the python script
echo "Starting Apartment Hunter at $(date)"
python main.py
echo "Finished at $(date)"
echo "-----------------------------------"

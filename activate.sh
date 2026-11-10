#!/bin/bash
# DRIFT Platform Activation Script

# Activate Python virtual environment
source venv/bin/activate

# Load environment variables if they exist (safely handle special characters)
if [[ -f ".env" ]]; then
    set -a  # automatically export all variables
    source .env
    set +a  # disable automatic export
fi

# Set Python path
export PYTHONPATH="$PWD:$PYTHONPATH"

echo "DRIFT Platform environment activated!"
echo ""
echo "Available commands:"
echo "  uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload  - Run backend"
echo "  ansible-playbook ansible/playbooks/...                          - Run Ansible playbooks"
echo ""
echo "To deactivate: deactivate"

#!/bin/bash
# Sync agent-mesh code to coreyllm
# Usage: ./tools/scripts/sync-coreyllm.sh

set -e

echo "🔍 Checking for running processes on coreyllm..."
if ssh mybox "pgrep -f 'orchestrator.main'" 2>/dev/null; then
    echo "⚠️  Agent-mesh process is running on coreyllm. Aborting."
    echo "   Wait for it to finish or manually stop it first."
    exit 1
fi

echo "📤 Pushing to GitHub..."
git push origin main

echo "📥 Pulling on coreyllm..."
ssh mybox "cd ~/agent-mesh && git stash 2>/dev/null; git pull origin main && git stash pop 2>/dev/null; echo '✅ Synced'"

echo "🔍 Verifying..."
ssh mybox "cd ~/agent-mesh && git log --oneline -1"

echo "✅ Done!"

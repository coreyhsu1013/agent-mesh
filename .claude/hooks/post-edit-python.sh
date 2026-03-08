#!/bin/bash
# Post-edit hook: validate Python syntax after Edit/Write on .py files
# Usage: triggered automatically by Claude Code after editing Python files

FILE="$1"

if [[ "$FILE" == *.py ]]; then
    python3 -m py_compile "$FILE" 2>&1
    if [ $? -ne 0 ]; then
        echo "❌ Syntax error in $FILE"
        exit 1
    fi
fi

#!/usr/bin/env bash
# Lists all KB markdown files that are candidates for curation review.
# Excludes docs/knowledge_base/mapped_folders/ (auto-generated from repos).
#
# Output: one file path per line, relative to the repo root.

KB_DIR="$(cd "$(dirname "$0")/../.." && pwd)/docs/knowledge_base"

find \
  "$KB_DIR/memory" \
  "$KB_DIR/skills" \
  $([ -d "$KB_DIR/docs" ] && echo "$KB_DIR/docs") \
  -name "*.md" \
  | sort \
  | sed "s|$(cd "$(dirname "$0")/../.." && pwd)/||"

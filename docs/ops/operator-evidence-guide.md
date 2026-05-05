# Operator Evidence Guide

## Purpose

Packet execution is only reviewable if operators return consistent evidence.

## Minimum Evidence

1. packet identifier
2. branch name
3. changed files
4. commands run
5. observed outputs
6. before/after API examples when behavior changed
7. screenshots for UI-related changes
8. risks remaining
9. rollback note

## Good Evidence Practices

1. capture exact commands rather than paraphrasing
2. include the smallest relevant output that proves the behavior
3. note any unexpected side effects or unrelated worktree changes
4. mention if docs were updated or intentionally deferred

## For UI Packets

1. capture the access path used
2. capture one success screenshot
3. capture one failure-mode screenshot if relevant

## For API Packets

1. include example request
2. include example response
3. highlight changed fields

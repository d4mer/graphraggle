# Rollback And Containment

## Purpose

This guide defines how to contain a bad packet rollout and preserve enough evidence to diagnose it.

## General Rules

1. Do not destroy runtime evidence before capturing it.
2. Do not overwrite state or uploads casually during investigation.
3. Prefer reverting the packet branch or deploy artifact over ad hoc edits.

## Containment Steps

1. Stop further rollout of dependent packets.
2. Capture current packet status, changed files, and runtime evidence.
3. Record failing commands and API responses.
4. Decide whether to revert code, redeploy known-good artifacts, or freeze only part of the workflow.

## Evidence To Preserve

1. packet branch and commit refs
2. relevant `podman` status output
3. failing gateway responses
4. failing worker or LightRAG observations
5. screenshots for UI regressions

## Suggested Rollback Questions

1. Did the packet change schema, runtime behavior, or only docs?
2. Is the issue isolated to one surface or system-wide?
3. Can the problem be contained without data loss?
4. Is a code revert enough, or is state migration involved?

## Do Not

1. delete uploads or state casually
2. reset the repo destructively
3. skip evidence capture to save time

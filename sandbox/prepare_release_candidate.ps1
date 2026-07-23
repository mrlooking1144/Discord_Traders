# Host-side preparation script — run OUTSIDE Windows Sandbox, on the
# development machine, BEFORE launching a Sandbox UAT session.
#
# Creates a separate, read-only-mappable checkout of the exact
# release-candidate commit using `git worktree`. This adds a worktree
# entry to this repository's shared Git metadata (.git/worktrees/...),
# but it does NOT read, modify, switch, or check out any branch in the
# live development working directory - no tracked file there is ever
# changed, and the development branch is never altered or switched.
#
# This script does not run automatically and is not invoked as part of
# planning/preparation — it is provided for use when UAT execution
# actually begins.
#
# IMPORTANT: $CommitHash's default value below is a PLACEHOLDER (the
# Milestone 2D.6 documentation commit). It MUST be replaced with the
# exact approved Milestone 2D.7 release-candidate commit — via the
# -CommitHash parameter or by editing the default — before this script
# is used for real UAT execution.

param(
    [string]$CommitHash = "ec7c0b12eabb6826536a8fd7f8822a6498ffaee7",  # PLACEHOLDER - replace before use
    [string]$Destination = "C:\DiscordTradersReleaseCandidate"
)

$RepoRoot = Split-Path -Parent $PSScriptRoot

function Stop-OnGitFailure {
    param([string]$Description)
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "ERROR: $Description failed (exit code $LASTEXITCODE)."
        Write-Host "Aborting - do not map any folder into Windows Sandbox until this is fixed."
        exit 1
    }
}

Write-Host "Verifying commit $CommitHash exists in this repository..."
git -C $RepoRoot cat-file -e "$CommitHash^{commit}" 2>$null
Stop-OnGitFailure "Commit verification (git cat-file -e $CommitHash^{commit})"

if (Test-Path $Destination) {
    Write-Host "An existing release-candidate checkout was found at $Destination ..."
    Write-Host "Attempting to remove it via 'git worktree remove' (safe: allowed to fail here; handled below)..."
    git -C $RepoRoot worktree remove --force $Destination 2>$null
    if (Test-Path $Destination) {
        Write-Host "'git worktree remove' did not fully remove the directory; removing it directly..."
        Remove-Item -Recurse -Force $Destination
    }
    Write-Host "Pruning any stale worktree administrative metadata..."
    git -C $RepoRoot worktree prune
    Stop-OnGitFailure "git worktree prune"
}

Write-Host "Creating a detached, read-only-mappable checkout of commit $CommitHash ..."
git -C $RepoRoot worktree add --detach $Destination $CommitHash
Stop-OnGitFailure "git worktree add --detach $Destination $CommitHash"

Write-Host "Resolving the exact commit checked out at $Destination ..."
$resolvedCommit = git -C $Destination rev-parse HEAD
Stop-OnGitFailure "git rev-parse HEAD (in $Destination)"

$requestedCommitFull = git -C $RepoRoot rev-parse $CommitHash
Stop-OnGitFailure "git rev-parse $CommitHash (in $RepoRoot)"

if ($resolvedCommit -ne $requestedCommitFull) {
    Write-Host ""
    Write-Host "ERROR: The checked-out commit ($resolvedCommit) does not match"
    Write-Host "the requested commit ($requestedCommitFull)."
    Write-Host "Aborting - do not use this checkout for UAT."
    exit 1
}

Write-Host ""
Write-Host "Verified: the checked-out commit matches the requested commit exactly."
Write-Host "Release-candidate checkout ready at: $Destination"
Write-Host "Commit: $resolvedCommit"
Write-Host ""
Write-Host "Next: map $Destination READ-ONLY as the Windows Sandbox"
Write-Host "MappedFolder HostFolder (see sandbox/discord_traders_uat.wsb)."
Write-Host ""
Write-Host "This operation added a 'git worktree' entry to this repository's"
Write-Host "shared Git metadata (.git/worktrees/...). It did NOT read, modify,"
Write-Host "switch, or check out any branch in the live development working"
Write-Host "directory, and no tracked file there was changed - only the"
Write-Host "worktree administrative entry was added, which the cleanup step"
Write-Host "below removes."
Write-Host ""
Write-Host "=== Cleanup after UAT is complete ==="
Write-Host "Once UAT finishes and this release-candidate checkout is no longer"
Write-Host "needed, run the following from the development machine (not inside"
Write-Host "Sandbox), from within this repository (e.g. cd '$RepoRoot' first):"
Write-Host ""
Write-Host "    git worktree remove C:\DiscordTradersReleaseCandidate"
Write-Host "    git worktree prune"
Write-Host ""

# The models

One TLA+ module per protocol under `specs/<area>/`, written from the code it describes and checked by TLC in every
interleaving its configs bound (ADR 0221). A module is checked once per `<Module>*.cfg` beside it, and a config's first
line says how (`scripts/tla_check.sh`): `EXPECT VIOLATION <Invariant>` (or `temporal`) must fail, the evidence that the
design rests on what it removes; `ON DEMAND` is too large for every run; anything else must hold.

## The gate's cost, and the rule

`make tla-check` runs every config but the on-demand ones; `make tla-check-full` runs them all. The owner's rule for
models (2026-11-18): small constants, a gate a person can wait for. So:

- **The whole gate finishes in about 12 minutes, and no gate config takes more than about 30 seconds**, on the machine the
  table below was measured on (four cores shared with other agents, TLC with two workers, `TLC_WORKERS=2`).
- A config over the bound is narrowed while its property is still exercised: its constants shrunk (fewer failures, fewer
  callers, Lance's measured read order rather than either), or split in two (a safety config with every invariant at the
  full constants, and a liveness config with one failure fewer, since checking a temporal property over the whole
  behaviour graph costs ten to twenty times the safety check of the same states), and the wide form kept beside it as
  `ON DEMAND` (`<Name>Full.cfg`, or the existing `<Name>Wide.cfg` where that already covers it).
- A must-fail config narrowed must still fail for its stated reason: its trace is read, not only its verdict.
- `make tla-check-full` runs once before a change to a model lands, with a limit per config (`TLC_TIMEOUT=30m`); a config
  that does not finish within it is reported as not finished, never as holding.
- `scripts/tla_check.sh` prints each config's distinct states and seconds, so the table is regenerated from its output.

## Every config

Measured 2026-11-18 and 2026-11-19: the gate configs by one `make tla-check` (`TLC_WORKERS=2`, 810 s in all), the on-demand ones by one `make tla-check-full` (`TLC_TIMEOUT=30m`), stopped before it reached every one: "not run" is a config that run did not reach, "not finished" one that ran past its 30 minutes. Neither holds nor fails.

| Config | Run | Verdict | Distinct states | Seconds |
| --- | --- | --- | --- | --- |
| `catalog/Cascade.cfg` | gate | holds | 721 | 1 s |
| `catalog/CascadeDestroy.cfg` | gate | holds | 13,221 | 2 s |
| `catalog/CascadeFaults.cfg` | gate | holds | 507,750 | 6 s |
| `catalog/CascadeNamespaceRecordAfterDrop.cfg` | gate | fails NothingIsLost as expected | 20 | 1 s |
| `catalog/CascadeNoPurgeCheck.cfg` | gate | fails LiveHasData as expected | 23 | 1 s |
| `catalog/CascadeNoRestrict.cfg` | gate | fails NoOrphan as expected | 30 | 1 s |
| `catalog/CascadeParentNotChecked.cfg` | gate | fails NoOrphan as expected | 41 | 1 s |
| `catalog/CascadeRootRecordAfterDrop.cfg` | gate | fails temporal as expected | 32 | 1 s |
| `catalog/CascadeTableFaults.cfg` | gate | holds | 4,281 | 2 s |
| `catalog/CascadeUndropClearsRedrop.cfg` | gate | fails NothingIsLost as expected | 172 | 1 s |
| `catalog/CascadeUndropNotResumable.cfg` | gate | fails temporal as expected | 33 | 2 s |
| `catalog/CascadeWarehouseLateTrash.cfg` | gate | fails NoTrashOutlivesItsWarehouse as expected | 260 | 1 s |
| `catalog/CommitAnnounce.cfg` | gate | holds | 5,176 | 1 s |
| `catalog/CommitAnnounceNoReconciler.cfg` | gate | fails temporal as expected | 1,476 | 2 s |
| `catalog/CommitAnnounceSlow.cfg` | gate | fails AnAuthoredWriteIsNotGivenUp as expected | 801 | 0 s |
| `catalog/CommitAnnounceUnauthored.cfg` | gate | fails temporal as expected | 4,152 | 2 s |
| `catalog/CommitPath.cfg` | gate | holds | 251,641 | 20 s |
| `catalog/CommitPathAnswerReplaced.cfg` | gate | fails AnswerStands as expected | 679 | 1 s |
| `catalog/CommitPathFinishNeedsPredecessor.cfg` | gate | fails temporal as expected | 31,219 | 4 s |
| `catalog/CommitPathFinishPassed.cfg` | gate | fails temporal as expected | 12,041 | 2 s |
| `catalog/CommitPathGap.cfg` | gate | holds | 440,234 | 10 s |
| `catalog/CommitPathGapFull.cfg` | on demand | holds | 440,234 | 78 s |
| `catalog/CommitPathGapLiveness.cfg` | gate | holds | 108,153 | 12 s |
| `catalog/CommitPathGapRefused.cfg` | gate | fails GoneIsTrue as expected | 75,763 | 3 s |
| `catalog/CommitPathKeyBody.cfg` | gate | fails SuccessIsForItsBody as expected | 97 | 1 s |
| `catalog/CommitPathKeys.cfg` | gate | holds | 89,745 | 10 s |
| `catalog/CommitPathLane.cfg` | gate | holds | 392,280 | 7 s |
| `catalog/CommitPathLaneLiveness.cfg` | gate | holds | 95,104 | 9 s |
| `catalog/CommitPathLaneWide.cfg` | on demand | holds | 777,559 | 81 s |
| `catalog/CommitPathLeaseInCommit.cfg` | gate | fails KeyAppliedOnce as expected | 2,094 | 2 s |
| `catalog/CommitPathNested.cfg` | gate | holds | 47 | 1 s |
| `catalog/CommitPathNestedUnjudged.cfg` | gate | fails OneRowPerKey as expected | 11 | 1 s |
| `catalog/CommitPathNoCAS.cfg` | gate | fails OneRowPerKey as expected | 132 | 1 s |
| `catalog/CommitPathNoReconciler.cfg` | gate | fails temporal as expected | 41,854 | 4 s |
| `catalog/CommitPathNoTurnLapse.cfg` | gate | fails temporal as expected | 39,748 | 4 s |
| `catalog/CommitPathOverwrite.cfg` | gate | holds | 12,293 | 3 s |
| `catalog/CommitPathOverwriteUnrenewed.cfg` | gate | fails KeyAppliedOnce as expected | 302 | 1 s |
| `catalog/CommitPathRebody.cfg` | gate | holds | 3,927 | 2 s |
| `catalog/CommitPathRefusalTakenOver.cfg` | gate | fails KeyedRefusalIsTrue as expected | 841 | 0 s |
| `catalog/CommitPathReleasedUnsearched.cfg` | gate | fails KeyAppliedOnce as expected | 11,490 | 2 s |
| `catalog/CommitPathRenewNotOwn.cfg` | gate | fails KeyAppliedOnce as expected | 1,315 | 1 s |
| `catalog/CommitPathRestore.cfg` | gate | holds | 12,293 | 3 s |
| `catalog/CommitPathRestoreUnrenewed.cfg` | gate | fails KeyAppliedOnce as expected | 271 | 1 s |
| `catalog/CommitPathSealAfterLanding.cfg` | gate | fails LandedDataSealed as expected | 28 | 1 s |
| `catalog/CommitPathSearchUnwhole.cfg` | gate | fails KeyAppliedOnce as expected | 112,388 | 3 s |
| `catalog/CommitPathTakeover.cfg` | gate | fails KeyAppliedOnce as expected | 411 | 0 s |
| `catalog/CommitPathTakeoverChain.cfg` | gate | fails KeyAppliedOnce as expected | 42,848 | 2 s |
| `catalog/CommitPathTakeoverCount.cfg` | gate | fails SuccessCountsWhatLanded as expected | 1,906 | 1 s |
| `catalog/CondemnedErasure.cfg` | gate | holds | 76,065 | 4 s |
| `catalog/CondemnedErasureFull.cfg` | on demand | holds | 76,065 | 79 s |
| `catalog/CondemnedErasureLiveness.cfg` | gate | holds | 21,048 | 18 s |
| `catalog/CondemnedErasureRefsInTheGrace.cfg` | gate | holds | 195,397 | 7 s |
| `catalog/CondemnedErasureRefsInTheGraceFull.cfg` | on demand | holds | 195,397 | 81 s |
| `catalog/CondemnedErasureRefsInTheGraceLiveness.cfg` | gate | holds | 18,036 | 7 s |
| `catalog/CondemnedErasureRestore.cfg` | gate | holds | 1,600 | 1 s |
| `catalog/CondemnedErasureUnpinned.cfg` | gate | fails PinsNameRetainedVersions as expected | 222 | 2 s |
| `catalog/Erasure.cfg` | gate | holds | 65,124 | 4 s |
| `catalog/ErasureClone.cfg` | gate | holds | 5,392 | 5 s |
| `catalog/ErasureCloneOutlives.cfg` | gate | fails temporal as expected | 619 | 2 s |
| `catalog/ErasureConcurrent.cfg` | gate | holds | 122,841 | 4 s |
| `catalog/ErasureConcurrentFull.cfg` | on demand | holds | 122,841 | 69 s |
| `catalog/ErasureConcurrentLiveness.cfg` | gate | holds | 21,286 | 9 s |
| `catalog/ErasureCopyTag.cfg` | gate | fails temporal as expected | 1,298 | 1 s |
| `catalog/ErasureCreatorLapsed.cfg` | gate | fails ACreatorIsToldBeforeTheRelease as expected | 3,675 | 2 s |
| `catalog/ErasureCreatorRace.cfg` | gate | fails ACreatorIsToldBeforeTheRelease as expected | 630 | 1 s |
| `catalog/ErasureCreatorStalled.cfg` | gate | fails ACreatorIsToldBeforeTheRelease as expected | 885 | 1 s |
| `catalog/ErasureCreatorUntold.cfg` | gate | fails ACreatorIsToldBeforeTheRelease as expected | 3,013 | 1 s |
| `catalog/ErasureCutBranch.cfg` | gate | fails temporal as expected | 1,718 | 2 s |
| `catalog/ErasureFinishMarkFirst.cfg` | gate | fails ARecordHasItsMark as expected | 618 | 1 s |
| `catalog/ErasureFinishOutsideHold.cfg` | gate | fails ARecordHasItsMark as expected | 12,623 | 2 s |
| `catalog/ErasureFull.cfg` | on demand | holds | 65,124 | 101 s |
| `catalog/ErasureLiveness.cfg` | gate | holds | 19,179 | 17 s |
| `catalog/ErasureMovesErased.cfg` | gate | holds | 27,552 | 6 s |
| `catalog/ErasureMovesInTheGrace.cfg` | gate | holds | 380,256 | 10 s |
| `catalog/ErasureNameTaking.cfg` | gate | fails ACreatorIsToldBeforeTheRelease as expected | 9,786 | 1 s |
| `catalog/ErasureNoCAS.cfg` | gate | fails ReleasedStaysReleased as expected | 6,062 | 2 s |
| `catalog/ErasureNoFence.cfg` | gate | fails PinsNameRetainedVersions as expected | 274 | 1 s |
| `catalog/ErasureNoPlanFirst.cfg` | gate | fails EveryReleasedBranchComesBack as expected | 887 | 1 s |
| `catalog/ErasureNoReread.cfg` | gate | fails temporal as expected | 543 | 1 s |
| `catalog/ErasureNoticeDeadline.cfg` | gate | fails NoticeOnlyForARecordedRelease as expected | 42,563 | 3 s |
| `catalog/ErasureNoticeLost.cfg` | gate | fails temporal as expected | 1,374 | 2 s |
| `catalog/ErasureNoticeNoReverify.cfg` | gate | holds | 1,077 | 2 s |
| `catalog/ErasureNoticeRace.cfg` | gate | holds | 276,620 | 7 s |
| `catalog/ErasureNoticeRaceWide.cfg` | on demand | holds | 1,394,272 | 997 s |
| `catalog/ErasureRefsInTheGrace.cfg` | gate | holds | 159,837 | 5 s |
| `catalog/ErasureRefsInTheGraceFull.cfg` | on demand | holds | 159,837 | 60 s |
| `catalog/ErasureRefsInTheGraceLiveness.cfg` | gate | holds | 17,080 | 6 s |
| `catalog/ErasureReleaseTakesCloneTag.cfg` | gate | fails NoCloneIsBroken as expected | 535 | 1 s |
| `catalog/ErasureRestore.cfg` | gate | fails temporal as expected | 198 | 1 s |
| `catalog/ErasureStalledDoors.cfg` | gate | holds | 10,881 | 4 s |
| `catalog/ErasureTagEventLost.cfg` | gate | fails temporal as expected | 715 | 2 s |
| `catalog/ErasureWide.cfg` | on demand | not finished |  | 1802 s |
| `catalog/Fence.cfg` | gate | holds | 4,096 | 2 s |
| `catalog/FenceHistory.cfg` | gate | holds | 22,528 | 1 s |
| `catalog/FenceLowerTier.cfg` | gate | holds | 43,776 | 2 s |
| `catalog/FenceNoConfirm.cfg` | gate | fails NoLastStepBesideAnExcluder as expected | 37 | 1 s |
| `catalog/FenceNoRecheck.cfg` | gate | fails Exclusive as expected | 14 | 0 s |
| `catalog/FenceSubdirectories.cfg` | gate | fails MarksAreTheCatalogs as expected | 41 | 1 s |
| `catalog/Grants.cfg` | gate | holds | 35,934 | 16 s |
| `catalog/GrantsAnswersUnread.cfg` | gate | fails GrantAnswersTruthfully as expected | 352 | 1 s |
| `catalog/GrantsBornReleased.cfg` | gate | fails BornLeftKept as expected | 2,591 | 2 s |
| `catalog/GrantsById.cfg` | gate | fails NoLeakAcrossIncarnations as expected | 1,530 | 1 s |
| `catalog/GrantsCachedCheck.cfg` | gate | fails NoAccessAfterRevoke as expected | 72 | 1 s |
| `catalog/GrantsChild.cfg` | on demand | holds | 143,976 | 57 s |
| `catalog/GrantsChildCascade.cfg` | gate | holds | 76,692 | 20 s |
| `catalog/GrantsChildLiveness.cfg` | gate | holds | 10,004 | 6 s |
| `catalog/GrantsChildReadFirst.cfg` | gate | fails ChildUnderLiveParent as expected | 9,184 | 1 s |
| `catalog/GrantsChildSafety.cfg` | gate | holds | 143,976 | 4 s |
| `catalog/GrantsChildTwice.cfg` | on demand | not finished |  | 1802 s |
| `catalog/GrantsChildrenBeforeMark.cfg` | gate | fails ChildUnderLiveParent as expected | 6,234 | 2 s |
| `catalog/GrantsClaimKept.cfg` | gate | fails temporal as expected | 19,633 | 4 s |
| `catalog/GrantsClaimUnaddressed.cfg` | gate | fails temporal as expected | 22,810 | 4 s |
| `catalog/GrantsConverge.cfg` | gate | holds | 33 | 1 s |
| `catalog/GrantsConvergeAdopts.cfg` | gate | fails GitRevokesOnlyItsOwn as expected | 22 | 1 s |
| `catalog/GrantsConvergeDies.cfg` | gate | fails GitRevokesOnlyItsOwn as expected | 28 | 2 s |
| `catalog/GrantsConvergeRecreate.cfg` | gate | holds | 58 | 1 s |
| `catalog/GrantsConvergeStale.cfg` | gate | fails temporal as expected | 58 | 1 s |
| `catalog/GrantsCreatingUncounted.cfg` | gate | fails ChildUnderLiveParent as expected | 9,241 | 2 s |
| `catalog/GrantsCreatingUnsettled.cfg` | gate | fails temporal as expected | 22,254 | 4 s |
| `catalog/GrantsDeadUnswept.cfg` | gate | fails temporal as expected | 18,132 | 4 s |
| `catalog/GrantsDeclared.cfg` | gate | holds | 55,604 | 3 s |
| `catalog/GrantsDeclaredFull.cfg` | on demand | holds | 1,397,283 | 27 s |
| `catalog/GrantsDeclaredLedger.cfg` | gate | holds | 3,681 | 3 s |
| `catalog/GrantsDeclaredStale.cfg` | gate | fails temporal as expected | 2,386 | 2 s |
| `catalog/GrantsDeleteTwice.cfg` | gate | holds | 653,120 | 13 s |
| `catalog/GrantsDeleteTwiceById.cfg` | gate | fails DeleteEndsOnlyItsOwn as expected | 32,258 | 2 s |
| `catalog/GrantsIntruder.cfg` | gate | holds | 447,964 | 9 s |
| `catalog/GrantsIntruderWrites.cfg` | gate | fails OnlyAuthorizedWrites as expected | 29 | 1 s |
| `catalog/GrantsLiveBeforeSeed.cfg` | gate | fails OwnedWhileLive as expected | 2 | 1 s |
| `catalog/GrantsPurgeUnstamped.cfg` | gate | fails PurgeTakesNothingLive as expected | 1,330 | 1 s |
| `catalog/GrantsRefusalStuck.cfg` | gate | fails RefusedDeleteLeavesLive as expected | 193 | 1 s |
| `catalog/GrantsResolvesDeleting.cfg` | gate | fails GrantAnswersTruthfully as expected | 326 | 1 s |
| `catalog/GrantsRevoke.cfg` | gate | holds | 599,748 | 12 s |
| `catalog/GrantsRevokeById.cfg` | gate | fails NoNarrowAcrossIncarnations as expected | 31,554 | 2 s |
| `catalog/GrantsTable.cfg` | gate | holds | 440 | 1 s |
| `catalog/GrantsTableByName.cfg` | gate | fails TableNoLeak as expected | 25 | 1 s |
| `catalog/GrantsTickFinishesOrphan.cfg` | gate | fails ChildUnderLiveParent as expected | 6,319 | 2 s |
| `catalog/GrantsUndropOutlasts.cfg` | gate | fails TableOwnedWhileLive as expected | 278 | 1 s |
| `catalog/TableLifecycle.cfg` | gate | holds | 243,416 | 7 s |
| `catalog/TableLifecycleCascade.cfg` | gate | holds | 234,291 | 7 s |
| `catalog/TableLifecycleCascadeFull.cfg` | on demand | holds | 234,291 | 34 s |
| `catalog/TableLifecycleCascadeLiveness.cfg` | gate | holds | 94,893 | 15 s |
| `catalog/TableLifecycleCascadeProtectRace.cfg` | gate | fails ProtectedNeverDestroyed as expected | 185 | 2 s |
| `catalog/TableLifecycleCommitOutlastsLease.cfg` | gate | fails OneCreateCommitsPerTable as expected | 690 | 1 s |
| `catalog/TableLifecycleCreate.cfg` | gate | holds | 167,698 | 5 s |
| `catalog/TableLifecycleCreateBothAnswered.cfg` | gate | fails OneCreateCommitsPerTable as expected | 393 | 2 s |
| `catalog/TableLifecycleCreateFull.cfg` | on demand | holds | 167,698 | 23 s |
| `catalog/TableLifecycleCreateLapse.cfg` | gate | fails OneCreateCommitsPerTable as expected | 612 | 1 s |
| `catalog/TableLifecycleCreateLiveness.cfg` | gate | holds | 61,232 | 10 s |
| `catalog/TableLifecycleCreateRace.cfg` | gate | fails AnsweredCreateStands as expected | 442 | 1 s |
| `catalog/TableLifecycleFull.cfg` | on demand | holds | 243,416 | 41 s |
| `catalog/TableLifecycleLiveness.cfg` | gate | holds | 101,648 | 16 s |
| `catalog/TableLifecycleNoFence.cfg` | gate | fails AnsweredCreateStands as expected | 3,611 | 1 s |
| `catalog/TableLifecycleNoNameCAS.cfg` | gate | fails NameLiveOnce as expected | 221 | 2 s |
| `catalog/TableLifecycleNoSettle.cfg` | gate | fails LiveHasData as expected | 632 | 1 s |
| `catalog/TableLifecycleNsUndropRace.cfg` | gate | fails UndropRestoresExactly as expected | 881 | 1 s |
| `catalog/TableLifecycleProtectRace.cfg` | gate | fails ProtectedNeverDestroyed as expected | 190 | 1 s |
| `catalog/TableLifecycleProtectUnchecked.cfg` | gate | fails ProtectedNeverDestroyed as expected | 141 | 1 s |
| `catalog/TableLifecycleRename.cfg` | gate | holds | 99,365 | 17 s |
| `catalog/TableLifecycleSoftDropWrongRow.cfg` | gate | fails RecoverableUntilPurged as expected | 17,782 | 2 s |
| `catalog/TableLifecycleStock.cfg` | gate | holds | 102,559 | 15 s |
| `catalog/TableLifecycleStockCreateRace.cfg` | gate | fails DeclarationIsItsDeclarers as expected | 82 | 1 s |
| `catalog/TableLifecycleStockDoors.cfg` | gate | holds | 135,286 | 19 s |
| `catalog/TableLifecycleStockIntruder.cfg` | gate | fails DeclarationIsItsDeclarers as expected | 104 | 1 s |
| `catalog/TableLifecycleStockLapse.cfg` | gate | fails OneCreateCommitsPerTable as expected | 1,439 | 1 s |
| `catalog/TableLifecycleStockRedeclare.cfg` | gate | fails DeclaredCredentialDiesWithItsFence as expected | 249 | 1 s |
| `catalog/TableLifecycleStockWide.cfg` | on demand | holds | 1,055,808 | 139 s |
| `catalog/TableLifecycleUndrop.cfg` | gate | holds | 234,695 | 8 s |
| `catalog/TableLifecycleUndropClearsRedrop.cfg` | gate | fails RecoverableUntilPurged as expected | 1,710 | 1 s |
| `catalog/TableLifecycleUndropFull.cfg` | on demand | holds | 234,695 | 34 s |
| `catalog/TableLifecycleUndropLiveness.cfg` | gate | holds | 103,269 | 17 s |
| `catalog/TableLifecycleUndropOverDestroyed.cfg` | gate | fails LiveHasData as expected | 4,571 | 2 s |
| `catalog/TableLifecycleUndropRace.cfg` | gate | fails UndropRestoresExactly as expected | 1,000 | 1 s |
| `catalog/TableLifecycleWide.cfg` | on demand | holds | 546,845 | 82 s |
| `catalog/WriteGovernance.cfg` | gate | holds | 11,791 | 1 s |
| `catalog/WriteGovernanceBranch.cfg` | gate | holds | 1,448 | 1 s |
| `catalog/WriteGovernanceJudgedLoose.cfg` | gate | fails ContractKept as expected | 23,217 | 1 s |
| `catalog/WriteGovernanceMarkAny.cfg` | gate | fails RollbackRestoresInForce as expected | 18,750 | 2 s |
| `catalog/WriteGovernanceOverwriteGap.cfg` | gate | fails RunRuleHeld as expected | 348 | 1 s |
| `catalog/WriteGovernanceOverwriteRebases.cfg` | gate | fails ContractKept as expected | 2,362 | 1 s |
| `catalog/WriteGovernanceOverwriteUnheld.cfg` | gate | fails OverwriteUnseen as expected | 73 | 1 s |
| `catalog/WriteGovernanceRedeclare.cfg` | gate | holds | 547 | 1 s |
| `catalog/WriteGovernanceRegisterUnjudged.cfg` | gate | fails RunRuleHeld as expected | 477 | 1 s |
| `catalog/WriteGovernanceRenameUnjudged.cfg` | gate | fails RunRuleHeld as expected | 489 | 0 s |
| `catalog/WriteGovernanceReplacedRead.cfg` | gate | fails RollbackRestoresInForce as expected | 1,222 | 1 s |
| `catalog/WriteGovernanceRestoreRefused.cfg` | gate | fails RollbackRestoresInForce as expected | 1,809 | 1 s |
| `catalog/WriteGovernanceRollbackAny.cfg` | gate | fails RollbackRestoresInForce as expected | 1,003 | 1 s |
| `catalog/WriteGovernanceRule.cfg` | gate | holds | 702 | 2 s |
| `catalog/WriteGovernanceSecondRead.cfg` | gate | fails ContractKept as expected | 537 | 0 s |
| `catalog/WriteGovernanceSets.cfg` | gate | holds | 650,613 | 8 s |
| `catalog/WriteGovernanceUndropUnchecked.cfg` | gate | fails RunRuleHeld as expected | 473 | 1 s |
| `catalog/WriteGovernanceUnheld.cfg` | gate | fails RunRuleHeld as expected | 423 | 1 s |
| `catalog/WriteGovernanceUnjudged.cfg` | gate | fails ContractKept as expected | 2,892 | 1 s |
| `catalog/WriteGovernanceUnstanding.cfg` | gate | fails RunRuleHeld as expected | 352 | 1 s |
| `catalog/WriteGovernanceWide.cfg` | gate | holds | 887,175 | 9 s |
| `lineage/EventPath.cfg` | gate | holds | 55,308 | 4 s |
| `lineage/EventPathBusForgets.cfg` | gate | fails NoLoss as expected | 54 | 1 s |
| `lineage/EventPathBusLimit.cfg` | gate | fails NoLoss as expected | 999 | 1 s |
| `lineage/EventPathPair.cfg` | gate | holds | 738,589 | 14 s |
| `maintenance/Reclaim.cfg` | gate | holds | 149,943 | 5 s |
| `maintenance/ReclaimBranchReuse.cfg` | gate | fails DeletesCondemned as expected | 6,147 | 2 s |
| `maintenance/ReclaimBranchReuseHold.cfg` | gate | fails RetentionKept as expected | 45,060 | 3 s |
| `maintenance/ReclaimBranchReuseTag.cfg` | gate | fails RefsWhole as expected | 50,265 | 3 s |
| `maintenance/ReclaimBranches.cfg` | gate | holds | 289,358 | 8 s |
| `maintenance/ReclaimBranchesTag.cfg` | gate | holds | 59,687 | 3 s |
| `maintenance/ReclaimBranchesWide.cfg` | on demand | not run |  |  |
| `maintenance/ReclaimCredentialOutlivesLicence.cfg` | gate | fails DeletesCondemned as expected | 8,530 | 2 s |
| `maintenance/ReclaimDoorReadsFirst.cfg` | gate | fails RefsWhole as expected | 1,551 | 1 s |
| `maintenance/ReclaimEntryDropped.cfg` | gate | fails HalfOnlyCondemned as expected | 355 | 1 s |
| `maintenance/ReclaimFork.cfg` | gate | holds | 232,014 | 7 s |
| `maintenance/ReclaimForkEntryDropped.cfg` | gate | fails HalfOnlyCondemned as expected | 182 | 1 s |
| `maintenance/ReclaimForkWide.cfg` | on demand | not run |  |  |
| `maintenance/ReclaimGoAny.cfg` | gate | fails RefsWhole as expected | 8,998 | 2 s |
| `maintenance/ReclaimHoldReadsFirst.cfg` | gate | fails RetentionKept as expected | 127 | 1 s |
| `maintenance/ReclaimLicenceCollected.cfg` | gate | fails DeletesCondemned as expected | 9,255 | 1 s |
| `maintenance/ReclaimListedElsewhere.cfg` | gate | fails CondemnsAsListed as expected | 938 | 1 s |
| `maintenance/ReclaimLive.cfg` | gate | holds | 3,899 | 3 s |
| `maintenance/ReclaimLiveDeadPin.cfg` | gate | holds | 3,502 | 2 s |
| `maintenance/ReclaimLiveNoUnpin.cfg` | gate | fails temporal as expected | 3,181 | 2 s |
| `maintenance/ReclaimLiveRestore.cfg` | gate | holds | 58,101 | 8 s |
| `maintenance/ReclaimLiveWide.cfg` | on demand | not run |  |  |
| `maintenance/ReclaimNamedEntries.cfg` | gate | fails DeletesCondemned as expected | 22,863 | 2 s |
| `maintenance/ReclaimNoCondemn.cfg` | gate | fails RefsWhole as expected | 2,534 | 1 s |
| `maintenance/ReclaimNoExplicitPins.cfg` | gate | fails RefsWhole as expected | 1,386 | 1 s |
| `maintenance/ReclaimPinSweepLive.cfg` | gate | fails RefsWhole as expected | 4,003 | 1 s |
| `maintenance/ReclaimPolicies.cfg` | gate | holds | 168,224 | 6 s |
| `maintenance/ReclaimPolicyBeforeCondemn.cfg` | gate | fails RetentionKept as expected | 65 | 1 s |
| `maintenance/ReclaimPromotedOnAGoneBranch.cfg` | gate | fails DeletesCondemned as expected | 7,167 | 2 s |
| `maintenance/ReclaimRelease.cfg` | gate | holds | 89,301 | 3 s |
| `maintenance/ReclaimReleaseSwept.cfg` | gate | fails RefsWhole as expected | 36,727 | 3 s |
| `maintenance/ReclaimReleaseUnpinned.cfg` | gate | fails RefsWhole as expected | 10,414 | 1 s |
| `maintenance/ReclaimReleaseWide.cfg` | on demand | not run |  |  |
| `maintenance/ReclaimReleaseWriter.cfg` | gate | holds | 368,101 | 12 s |
| `maintenance/ReclaimRollbackAfterDelete.cfg` | gate | fails HalfOnlyCondemned as expected | 306 | 1 s |
| `maintenance/ReclaimSweep.cfg` | gate | holds | 207,462 | 6 s |
| `maintenance/ReclaimTagsFirst.cfg` | gate | holds | 52,723 | 3 s |
| `maintenance/ReclaimTwo.cfg` | gate | holds | 426,270 | 12 s |
| `maintenance/ReclaimTwoFaults.cfg` | on demand | holds | 3,550,974 | 206 s |
| `maintenance/ReclaimTwoRefs.cfg` | gate | holds | 429,952 | 15 s |
| `maintenance/ReclaimTwoRefsWide.cfg` | on demand | not run |  |  |
| `maintenance/ReclaimTwoWide.cfg` | on demand | holds | 11,699,903 | 495 s |
| `maintenance/ReclaimVDeleteReadsFirst.cfg` | gate | fails VDeleteSparesTags as expected | 32 | 1 s |
| `maintenance/ReclaimWide.cfg` | on demand | not run |  |  |
| `maintenance/ReclaimWriter.cfg` | gate | holds | 137,345 | 4 s |
| `maintenance/ReclaimWriterOutlivesAge.cfg` | gate | fails InFlightKept as expected | 71 | 1 s |
| `maintenance/WorkQueue.cfg` | gate | holds | 212,664 | 3 s |
| `maintenance/WorkQueueAckFirst.cfg` | gate | fails HeldUnlessBackstopped as expected | 702 | 1 s |
| `maintenance/WorkQueueDueLimit.cfg` | gate | fails ACommitIsCovered as expected | 47 | 2 s |
| `maintenance/WorkQueueDueNak.cfg` | gate | fails AWaitingFiringHoldsNoSlot as expected | 26 | 1 s |
| `maintenance/WorkQueueLateSchedule.cfg` | gate | fails HeldUnlessBackstopped as expected | 128 | 1 s |
| `maintenance/WorkQueueLive.cfg` | gate | holds | 9,640 | 6 s |
| `maintenance/WorkQueueLiveWide.cfg` | on demand | not run |  |  |
| `maintenance/WorkQueueNoAudit.cfg` | gate | fails temporal as expected | 2,544 | 2 s |
| `maintenance/WorkQueueNoClaim.cfg` | gate | fails OnePassPerTable as expected | 1,623 | 1 s |
| `maintenance/WorkQueuePaused.cfg` | gate | fails OnePassPerTable as expected | 662 | 1 s |
| `maintenance/WorkQueueReholdFollowUpUnguarded.cfg` | gate | fails HeldUnlessBackstopped as expected | 6,006 | 2 s |
| `maintenance/WorkQueueUndrop.cfg` | gate | holds | 237,335 | 5 s |
| `maintenance/WorkQueueUndropFollowUp.cfg` | gate | fails HeldUnlessBackstopped as expected | 706 | 1 s |
| `maintenance/WorkQueueUndropLive.cfg` | gate | holds | 22,762 | 11 s |
| `maintenance/WorkQueueWide.cfg` | on demand | not run |  |  |

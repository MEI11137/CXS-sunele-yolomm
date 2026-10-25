# Release notes draft: Video frame datasets and manual splitting

> Draft only. No version number or publication date is assigned here; those are supplied by the repository's Git-based build metadata. This document has not been published as a GitHub Release.

This update adds an optional workflow for turning video-detection input into a named dataset draft. Detection can continue without dataset generation, and dataset-generation failures are tracked separately from the video-detection result.

## What's included

- Administrators can optionally generate a dataset draft while submitting a video detection job. The default remains off.
- RGB video is the sampling timeline. A positive frame interval `N` selects source frames `0, N, 2N, ...`. The generated images retain the original frame pixels; detection boxes are not drawn into them.
- A compatible published model can be chosen for annotations. The detection model is used as the default only when its modality is compatible. No visual-language-model review is run for each extracted frame.
- Single-modal RGB drafts and paired RGB-plus-secondary-modality drafts are supported. Multimodal pairing uses the configured signed time offset and nearby readable frames. Unmatched RGB samples remain in the draft for repair or exclusion rather than being silently dropped or fabricated.
- Every generated sample starts unassigned. Administrators can preview samples, inspect annotations and pairing state, upload a missing secondary image, and move selected samples to train, validation, test, excluded, or back to unassigned.
- Finalization requires every retained sample to be assigned, non-empty train and validation splits, and complete required files/modalities. Test data is optional. Excluded samples are omitted from the final archive.
- Confirmed feedback samples now produce a draft for the same manual review and splitting workflow. They are no longer copied into train/validation automatically.
- Drafts and their samples are stored separately from formal trainable datasets; once finalized, the existing dataset ID and name are used and the list avoids showing a duplicate draft entry.

## Workflow

1. In video detection, an administrator enables dataset generation, names the draft, selects a sampling interval and compatible annotation model, and—when needed—selects the secondary video, modality, and time offset.
2. Open the resulting draft from **Dataset Management → Manual Split**. Review previews, assign the samples, and repair or exclude missing secondary frames.
3. Finalize after train and validation samples are present and the remaining samples meet the validation rules. The system builds the standard YOLO dataset archive from the explicit assignments.

## Verification

- Backend suite excluding the existing browser-video-writer test: 157 passed, 4 dependency/network warnings, exit code 0. The video-writer test passes by itself (exit code 0), and logs that the OpenH264 DLL is unavailable before exercising its codec fallback. When all 158 tests run in one process, pytest reports 158 passed but Windows exits with code 1 after the summary and has emitted an OpenCV native exception in that test; the combined-suite exit issue remains to be investigated.
- Synthetic end-to-end fixture: generated short RGB and thermal videos at different frame rates, checked interval-based extraction and timestamp pairing, retained unmatched frames, manually assigned samples, finalized the archive, and verified that excluded samples were omitted and no training sample was copied into validation.
- Frontend build-info tests: 7 passed.
- Vue production build: passed.

## Notes

- Auto-labels come from the selected detection model. Review them before using the finalized data for training.
- Missing secondary frames are intentionally not inferred or synthesized. They must be repaired with a real corresponding image or excluded.
- The workflow does not randomly split data; the administrator makes the split decisions.

# Video Frame Dataset Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let administrators generate named single-modal or paired multimodal draft datasets while processing uploaded videos, manually assign samples to train/val/test, and finalize validated YOLO archives without fabricated validation samples.

**Architecture:** Add separate draft and draft-sample tables so incomplete datasets never violate the existing non-null ZIP contract. Video and feedback producers create draft samples; one shared service owns assignment, repair, and finalization into the existing approved `datasets` table. Vue adds conditional video-generation controls and an administrator-only sample splitting page.

**Tech Stack:** Python 3, Flask, SQLite, OpenCV, project-owned Ultralytics/YOLOMM, pytest, Vue 3, TypeScript, Element Plus, Vite

## Global Constraints

- “同时生成数据集” is administrator-only and defaults off.
- Frame interval N means source frame indices `0, N, 2N, ...`; N must be a positive integer.
- Annotation model defaults to the video model only when modality-compatible and remains independently selectable.
- Single-modal drafts use one RGB video; multimodal drafts use RGB as the master timeline and a second video with optional signed time offset.
- Missing secondary frames remain repairable draft samples and are never silently dropped or fabricated.
- Extracted images are original pixels without rendered boxes; every complete sample has a YOLO label object, including an empty label for background images.
- No automatic visual-language-model review runs during video extraction.
- Drafts do not contain an automatic train/val/test split; administrators assign every non-excluded sample manually.
- Finalization requires non-empty train and val groups, allows an empty test group, and rejects unassigned, incomplete, or unlabeled samples.
- Existing feedback export must create a draft and must not copy the first training sample into validation.
- Existing video detection remains successful when optional dataset generation fails.
- Remote tag, push, and GitHub Release creation remain outside scope without separate user confirmation.

---

### Task 1: Draft Dataset Schema and Domain Types

**Files:**
- Modify: `web-flask/utils/schema_migrations.py`
- Modify: `web-flask/tests/test_schema_migrations.py`
- Create: `web-flask/services/dataset_draft_service.py`
- Create: `web-flask/tests/test_dataset_draft_service.py`

**Interfaces:**
- Produces tables `dataset_drafts` and `dataset_draft_samples`.
- Produces constants `DRAFT_STATES`, `SAMPLE_SPLITS`, `PAIRING_STATES`.
- Produces dataclasses `DraftDatasetResult` and `DraftSampleResult` with `to_dict()`.

- [ ] **Step 1: Write failing migration assertions**

Extend `test_legacy_schema_is_upgraded_idempotently_without_data_loss` to assert both new tables and the unique draft-name/sample-key indexes. Assert columns including `workflow_status`, `source_type`, `metadata_json`, `split`, image/label object references, timestamps, pairing state, and annotations JSON.

- [ ] **Step 2: Verify the migration test fails for missing tables**

Run: `python -m pytest tests/test_schema_migrations.py -q`

Expected: FAIL because `dataset_drafts` and `dataset_draft_samples` do not exist.

- [ ] **Step 3: Add idempotent tables and indexes**

Create tables inside `run_schema_migrations()` with foreign keys only to stable existing entities (`user` and `video_detection_records` references remain nullable). Add:

```sql
CREATE UNIQUE INDEX uq_dataset_draft_name ON dataset_drafts(name);
CREATE UNIQUE INDEX uq_dataset_draft_sample_key
ON dataset_draft_samples(draft_id, sample_key);
CREATE INDEX ix_dataset_draft_sample_split
ON dataset_draft_samples(draft_id, split);
```

Use text workflow values `generating`, `draft`, `failed`, `finalized` and split values `unassigned`, `train`, `val`, `test`, `excluded`.

- [ ] **Step 4: Add domain dataclasses and constants**

Create `dataset_draft_service.py` with no Flask imports. Define exact public constants and serializable result objects used by services and routes.

- [ ] **Step 5: Run migration tests**

Run: `python -m pytest tests/test_schema_migrations.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the schema slice**

```bash
git add web-flask/utils/schema_migrations.py web-flask/tests/test_schema_migrations.py web-flask/services/dataset_draft_service.py
git commit -m "feat: add draft dataset schema"
```

### Task 2: Draft Creation, Assignment, and Validation Service

**Files:**
- Modify: `web-flask/services/dataset_draft_service.py`
- Modify: `web-flask/tests/test_dataset_draft_service.py`

**Interfaces:**
- Produces `ensure_dataset_name_available(name, database, exclude_draft_id=None) -> None`.
- Produces `create_draft(config, database) -> DraftDatasetResult`.
- Produces `add_draft_sample(draft_id, sample, database) -> DraftSampleResult`.
- Produces `assign_draft_samples(draft_id, sample_ids, split, database) -> dict`.
- Produces `validate_draft_for_finalization(draft_id, database) -> dict`.
- Produces `mark_draft_failed(draft_id, message, database) -> None`.

- [ ] **Step 1: Write failing service tests**

Use an in-memory SQLite fixture with both formal and draft tables. Cover literals for:

```python
assert create_draft(single_config, db).workflow_status == "generating"
with pytest.raises(DraftValidationError, match="名称已存在"):
    create_draft(duplicate_name_config, db)
assert assign_draft_samples("d1", ["s1", "s2"], "train", db)["updated"] == 2
```

Also test invalid split, cross-draft sample IDs, transaction rollback, empty train, empty val, unassigned samples, missing secondary modality, missing labels, excluded-sample exemption, and an allowed empty label object.

- [ ] **Step 2: Verify service tests fail for missing functions**

Run: `python -m pytest tests/test_dataset_draft_service.py -q`

Expected: FAIL on missing service APIs.

- [ ] **Step 3: Implement creation and global name uniqueness**

Query both `datasets` and `dataset_drafts`. Store metadata as UTF-8 JSON. Insert drafts with `generating` state and `unassigned` sample rows with stable UUID IDs.

- [ ] **Step 4: Implement atomic assignment**

Accept only the five declared split values, reject empty ID lists, prove every ID belongs to the requested draft, and update all rows in one transaction.

- [ ] **Step 5: Implement finalization validation**

Return literal split counts and class metadata. Reject all invariant violations from the spec with concise Chinese messages. Treat `label_object_key` with empty file content as present; only a missing object reference is invalid.

- [ ] **Step 6: Run focused tests**

Run: `python -m pytest tests/test_dataset_draft_service.py -q`

Expected: PASS.

- [ ] **Step 7: Commit draft business rules**

```bash
git add web-flask/services/dataset_draft_service.py web-flask/tests/test_dataset_draft_service.py
git commit -m "feat: manage draft dataset samples"
```

### Task 3: Finalize Drafts into Standard YOLO Archives

**Files:**
- Create: `web-flask/services/dataset_finalization_service.py`
- Create: `web-flask/tests/test_dataset_finalization_service.py`
- Modify: `web-flask/services/dataset_contract.py`
- Modify: `web-flask/tests/test_dataset_contract.py`

**Interfaces:**
- Consumes `validate_draft_for_finalization()`.
- Produces `finalize_draft(draft_id, work_dir, database, storage_client) -> FinalizedDatasetResult`.
- Formal archives remain accepted by `validate_dataset_archive()`.

- [ ] **Step 1: Write failing archive tests**

Create real temporary image and label bytes through a fake storage client. Assert exact archive members for single-modal and multimodal drafts:

```python
assert "images/train/a.jpg" in members
assert "labels/train/a.txt" in members
assert "images_thermal/val/b.jpg" in members
assert "images/val/a.jpg" not in members  # no fabricated copy
```

Assert `data.yaml`, `manifest.json`, successful formal dataset insertion, draft `finalized` state, rollback, and uploaded-ZIP deletion when the database insert fails.

- [ ] **Step 2: Verify archive tests fail for missing finalizer**

Run: `python -m pytest tests/test_dataset_finalization_service.py -q`

Expected: FAIL because the finalization service does not exist.

- [ ] **Step 3: Build archive from explicit assignments**

Download image and label objects for non-excluded samples only. Use shared sample basenames across RGB, X, and labels. Emit test directories only when test samples exist. Generate class names from draft metadata and include provenance in `manifest.json`.

- [ ] **Step 4: Validate, upload, and register atomically**

Call `validate_dataset_archive`, upload to `datasets`, insert an `APPROVED` formal row using the draft ID/name, then mark the draft finalized. Compensate by deleting the new ZIP on database failure.

- [ ] **Step 5: Run focused and contract tests**

Run: `python -m pytest tests/test_dataset_finalization_service.py tests/test_dataset_contract.py -q`

Expected: PASS.

- [ ] **Step 6: Commit finalization**

```bash
git add web-flask/services/dataset_finalization_service.py web-flask/tests/test_dataset_finalization_service.py web-flask/services/dataset_contract.py web-flask/tests/test_dataset_contract.py
git commit -m "feat: finalize manually split datasets"
```

### Task 4: Convert Feedback Export into a Draft Producer

**Files:**
- Modify: `web-flask/services/feedback_dataset_service.py`
- Modify: `web-flask/tests/test_feedback_dataset_service.py`
- Modify: `web-flask/routes/dataset.py`
- Modify: `web-flask/tests/test_dataset_routes.py`

**Interfaces:**
- Replaces direct `create_feedback_dataset_version()` archive creation with `create_feedback_dataset_draft()`.
- Returns `DraftDatasetResult` with `workflow_status == "draft"`.
- Reuses confirmed `corrected_annotations`; never uses raw AI-review suggestions as final labels.

- [ ] **Step 1: Replace old expectations with failing draft expectations**

Tests must assert that confirmed samples produce draft sample rows with `unassigned` split and label objects, no new formal `datasets` row exists, and no train/val duplicate is created. Multimodal feedback samples retain both modality references and one shared label.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_feedback_dataset_service.py tests/test_dataset_routes.py -q`

Expected: FAIL because the route still creates a formal feedback version.

- [ ] **Step 3: Implement feedback draft production**

Create one named feedback draft, add confirmed records in deterministic order, upload YOLO labels generated from normalized corrected annotations, and reuse existing image object references. Mark the draft `draft` only after every sample is stored.

- [ ] **Step 4: Update the feedback route contract**

Keep `POST /api/dataset/<id>/feedback-version` for compatibility, but return draft fields and message `反馈样本已生成待划分数据集`.

- [ ] **Step 5: Run feedback tests**

Run: `python -m pytest tests/test_feedback_dataset_service.py tests/test_dataset_routes.py -q`

Expected: PASS.

- [ ] **Step 6: Commit feedback behavior**

```bash
git add web-flask/services/feedback_dataset_service.py web-flask/tests/test_feedback_dataset_service.py web-flask/routes/dataset.py web-flask/tests/test_dataset_routes.py
git commit -m "fix: require manual split for feedback datasets"
```

### Task 5: Video Frame Extraction, Pairing, and Auto-Labeling

**Files:**
- Create: `web-flask/services/video_dataset_service.py`
- Create: `web-flask/tests/test_video_dataset_service.py`
- Modify: `web-flask/routes/video_detection.py`
- Create: `web-flask/tests/test_video_dataset_routes.py`
- Modify: `web-flask/utils/schema_migrations.py`

**Interfaces:**
- Produces `VideoDatasetConfig.from_form(form) -> VideoDatasetConfig`.
- Produces `selected_frame_indices(total_frames, interval) -> range`.
- Produces `nearest_secondary_frame(primary_index, primary_fps, secondary_fps, offset_seconds) -> int`.
- Produces `generate_video_dataset(draft_id, config, paths, database, storage_client) -> dict`.
- Video records gain nullable draft status/id/statistics columns through migration.

- [ ] **Step 1: Write failing pure scheduling and pairing tests**

Use literal expectations:

```python
assert list(selected_frame_indices(7, 1)) == [0, 1, 2, 3, 4, 5, 6]
assert list(selected_frame_indices(7, 2)) == [0, 2, 4, 6]
assert nearest_secondary_frame(25, 25.0, 30.0, 0.5) == 45
```

Cover signed offsets, clamping, invalid interval/FPS, nearby readable-frame fallback, and missing-secondary preservation.

- [ ] **Step 2: Write failing route/config tests**

Assert normal users cannot enable generation, admin requests require unique name and compatible model, single mode rejects an unexpected second-video requirement, multimodal mode requires a second video and modality, and the annotation model defaults only when compatible.

- [ ] **Step 3: Verify RED**

Run: `python -m pytest tests/test_video_dataset_service.py tests/test_video_dataset_routes.py -q`

Expected: FAIL because config, pairing, and route support are absent.

- [ ] **Step 4: Implement form parsing and model compatibility**

Read `generate_dataset`, `dataset_name`, `dataset_description`, `dataset_mode`, `frame_interval`, `annotation_model_id`, `annotation_confidence`, `secondary_modality`, `secondary_time_offset`, and `secondary_video`. Load annotation-model metadata including mode and modality before starting the process.

- [ ] **Step 5: Implement extraction and storage**

Use RGB frame indices `0, N, 2N...`. Save original encoded frames, locate paired X frames by timestamp plus offset with nearby search, upload image and label objects, and insert draft samples. For multimodal inference, call project-owned `load_detector`, `resolve_inference_inputs`, and `predict_image`; for single-modal inference, use the same normalized runtime.

- [ ] **Step 6: Keep detection and draft states independent**

Extend the background worker with a serialized dataset config. A dataset exception calls `mark_draft_failed` and updates video dataset status without changing a completed video detection status. Persist extracted, paired, missing, and maximum-error statistics.

- [ ] **Step 7: Run video-dataset tests**

Run: `python -m pytest tests/test_video_dataset_service.py tests/test_video_dataset_routes.py tests/test_schema_migrations.py -q`

Expected: PASS.

- [ ] **Step 8: Commit video generation backend**

```bash
git add web-flask/services/video_dataset_service.py web-flask/tests/test_video_dataset_service.py web-flask/routes/video_detection.py web-flask/tests/test_video_dataset_routes.py web-flask/utils/schema_migrations.py
git commit -m "feat: generate draft datasets from videos"
```

### Task 6: Administrator Draft Management APIs

**Files:**
- Modify: `web-flask/routes/dataset.py`
- Modify: `web-flask/tests/test_dataset_routes.py`
- Modify: `web-vue/src/api/dataset.ts`

**Interfaces:**
- Produces `GET /api/dataset/drafts/<draft_id>/samples`.
- Produces `POST /api/dataset/drafts/<draft_id>/assign`.
- Produces `POST /api/dataset/drafts/<draft_id>/samples/<sample_id>/secondary`.
- Produces `POST /api/dataset/drafts/<draft_id>/finalize`.
- Produces `POST /api/dataset/drafts/<draft_id>/retry`.
- Produces authenticated sample-image/label preview endpoints.
- Extends `/api/dataset/list` with `record_kind` and `workflow_status`.

- [ ] **Step 1: Write failing route authorization and response tests**

Assert 403 for normal users, pagination/filter output, atomic assignment payload, valid preview MIME type, repair ownership, finalization response, retry restrictions, and unified list de-duplication of finalized drafts.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_dataset_routes.py -q`

Expected: FAIL for missing routes and fields.

- [ ] **Step 3: Implement route handlers**

Keep Flask handlers thin: parse payload, call service, translate `DraftValidationError` to 400, missing records to 404, and permission failures to 403. Stream object bytes with a safe derived filename; never accept bucket/object keys from the client.

- [ ] **Step 4: Add TypeScript contracts**

Define `DatasetListItem`, `DatasetDraft`, `DatasetDraftSample`, `DraftSampleSplit`, assignment/finalization responses, and API methods. Keep formal `Dataset` compatibility for model-management consumers.

- [ ] **Step 5: Run route tests and TypeScript build**

Run: `python -m pytest tests/test_dataset_routes.py -q`

Run: `npm run build` from `web-vue`.

Expected: both pass.

- [ ] **Step 6: Commit API slice**

```bash
git add web-flask/routes/dataset.py web-flask/tests/test_dataset_routes.py web-vue/src/api/dataset.ts
git commit -m "feat: expose draft dataset management APIs"
```

### Task 7: Video Form Controls and Progress

**Files:**
- Modify: `web-vue/src/views/video-detection/VideoDetection.vue`
- Modify: `web-vue/src/api/video_detection.ts`
- Modify: `web-flask/tests/test_frontend_video_dataset_contract.py`

**Interfaces:**
- Consumes published model metadata and video draft request fields.
- Produces administrator-only conditional controls and dataset-generation progress.

- [ ] **Step 1: Write a failing frontend contract test**

Exercise the built request behavior through an extracted pure form helper or existing frontend contract-test pattern. Assert disabled generation sends no dataset fields, interval 2 is preserved, multimodal requests include second video/offset, and incompatible default annotation model becomes empty.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_frontend_video_dataset_contract.py -q`

Expected: FAIL because the controls/helper do not exist.

- [ ] **Step 3: Add conditional administrator controls**

Use `useUserStore().isAdmin()`. Add switch, name, description, mode, interval, annotation model, annotation confidence, secondary modality/video, offset, estimated count, and compatibility filtering. Reset hidden multimodal fields when switching to single mode.

- [ ] **Step 4: Submit optional fields and show independent progress**

Append the exact backend field names only when enabled. Display dataset state, extracted/paired/missing counts, draft ID link, and a warning when video succeeded but dataset failed.

- [ ] **Step 5: Run contract test and build**

Run: `python -m pytest tests/test_frontend_video_dataset_contract.py -q`

Run: `npm run build` from `web-vue`.

Expected: PASS.

- [ ] **Step 6: Commit video UI**

```bash
git add web-vue/src/views/video-detection/VideoDetection.vue web-vue/src/api/video_detection.ts web-flask/tests/test_frontend_video_dataset_contract.py
git commit -m "feat: configure dataset generation from video"
```

### Task 8: Manual Sample Splitting Page

**Files:**
- Create: `web-vue/src/views/admin/DatasetDraftSplit.vue`
- Modify: `web-vue/src/views/admin/DatasetManagement.vue`
- Modify: `web-vue/src/router/index.ts`
- Modify: `web-flask/tests/test_frontend_dataset_draft_contract.py`

**Interfaces:**
- Consumes draft list/sample/assignment/repair/finalization APIs.
- Produces admin route `/admin/datasets/:draftId/split`.

- [ ] **Step 1: Write failing frontend contract coverage**

Assert the route requires admin, dataset rows branch on `record_kind`, draft actions navigate to the split page, and finalization controls are disabled while unassigned or incomplete samples remain.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_frontend_dataset_draft_contract.py -q`

Expected: FAIL because the view and route are missing.

- [ ] **Step 3: Extend dataset list states and actions**

Show generating, draft, failed, and trainable labels. Draft rows expose “人工划分”; failed rows expose “重试”; only formal rows expose download and feedback-version actions. Update the feedback success message to identify a draft rather than a trainable version.

- [ ] **Step 4: Implement split page**

Add paginated sample cards/table with RGB/X previews, label count, class names, frame number, pairing state, filters, selection, batch split buttons, move-to-unassigned, exclude, and missing-X upload. Refresh counts after each mutation.

- [ ] **Step 5: Implement finalization UX**

Display live counts for unassigned/train/val/test/excluded/missing. Require explicit confirmation, call finalization, show backend invariant messages, and return to dataset list on success.

- [ ] **Step 6: Run frontend contract tests and build**

Run: `python -m pytest tests/test_frontend_dataset_draft_contract.py -q`

Run: `npm run build` from `web-vue`.

Expected: PASS.

- [ ] **Step 7: Commit manual split UI**

```bash
git add web-vue/src/views/admin/DatasetDraftSplit.vue web-vue/src/views/admin/DatasetManagement.vue web-vue/src/router/index.ts web-flask/tests/test_frontend_dataset_draft_contract.py
git commit -m "feat: add manual dataset splitting workflow"
```

### Task 9: Release Notes and Full Verification

**Files:**
- Create: `docs/releases/next-video-frame-datasets.md`
- Verify all modified files

**Interfaces:**
- Produces user-requested GitHub Release description draft without publishing it.

- [ ] **Step 1: Run the complete backend suite**

Run: `python -m pytest -q` from `web-flask` with the project runtime.

Expected: zero failures.

- [ ] **Step 2: Run frontend tests and production build**

Run: `npm run test:build-info`

Run: `npm run build`

Expected: all tests and build pass.

- [ ] **Step 3: Run synthetic end-to-end fixtures**

Generate short RGB and thermal fixture videos with different FPS. Verify N=1 and N=2 sample counts, timestamp pairing, missing-secondary preservation, manual split, standard archive validation, and absence of duplicated validation samples.

- [ ] **Step 4: Write factual Release notes**

Document shipped UI, single/multimodal workflow, model compatibility, manual split, feedback behavior change, migration, tests actually executed, and known ByteTrack/auto-label limitations. Do not invent a version number or claim remote publication.

- [ ] **Step 5: Inspect repository state**

Run: `git diff --check`

Run: `git status --short`

Confirm generated build files, temporary videos, databases, model weights, and extracted images are not tracked.

- [ ] **Step 6: Commit release notes**

```bash
git add docs/releases/next-video-frame-datasets.md
git commit -m "docs: add video dataset release notes"
```

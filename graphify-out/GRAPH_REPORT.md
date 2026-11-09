# Graph Report - fd-text-core  (2026-11-08)

## Corpus Check
- 105 files · ~67,861 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 6 file(s) not represented in the graph (top: (none) 2, .ini 1, .mako 1)

## Summary
- 1990 nodes · 4059 edges · 95 communities (63 shown, 32 thin omitted)
- Extraction: 97% EXTRACTED · 3% INFERRED · 0% AMBIGUOUS · INFERRED: 131 edges (avg confidence: 0.88)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `c43d227b`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- cost.py
- merge.py
- cjk_tsquery
- test_kg_tools.py
- parse
- eval.py
- conformance_gate.py
- test_gov_parser.py
- statute_layer.py
- lane.py
- connectors.py
- papers/harvest.py
- llm.py
- screenplay/harvest.py
- containers
- runner.py
- pathlib
- test_kg_lane_merge_wiring.py
- querylib.py
- re
- with
- query.py
- registry.py
- TestPresets
- test_ingest_contract.py
- test_kg_lane_kernel.py
- test_screenplay.py
- test_tool_annotations.py
- test_kg_lane_config.py
- test_keyword_ranking.py
- Embedder
- steps
- kg_lane_run.py
- embed_worker.py
- Grade
- test_eval.py
- db.py
- KGLaneQuery
- test_news_versioning.py
- spec
- build_statute_graph
- kg_lane/config.py
- test_kg_statute_layer.py
- chunk_text
- Any
- server.py
- selfcheck_surface.py
- test_pure.py
- scale_smoke.py
- DatasetError
- GraphitiQueryBackend
- kg_statute_repair_names.py
- asyncio
- papers/parser.py
- MergingLLMClient
- parse_doc_numbers
- TestVerticalLoading
- load
- _FakeBackend
- annotations.py
- list_verticals
- EvalReport
- json
- SlidingWindow
- _has_nul
- attach_name_embeddings
- backfill_gov_facets.py
- _split_sentences
- verify_vector_dual_read.py
- test_issuer_normalization_covers_real_variants
- fetch_one
- test_cli_seed_order_sql_params
- test_empty_both_paths_is_rejected_by_doc_validation
- TestEmbedderDimensions
- test_doc_leaves_clean_text_untouched_and_silent
- _CloningDriver
- FakeBoltWriter
- ChunkStrategy
- parse_uri
- ScreenplayParser
- test_dedup_query_pins_current_status
- fd-text-core
- test_rrf_weights_none_equals_explicit_ones
- test_rrf_vector_weight_promotes_vector_only_candidate
- test_rrf_weights_length_mismatch_raises
- test_config_fingerprint_includes_weights
- test_builtin_verticals_have_no_runtime_switch

## God Nodes (most connected - your core abstractions)
1. `from_dict()` - 33 edges
2. `_parse()` - 28 edges
3. `CostLedger` - 24 edges
4. `Embedder` - 24 edges
5. `get()` - 24 edges
6. `parse()` - 23 edges
7. `build_statute_graph()` - 23 edges
8. `KGLaneQuery` - 22 edges
9. `parse_gov_doc()` - 22 edges
10. `_plan()` - 22 edges

## Surprising Connections (you probably didn't know these)
- `doc()` --calls--> `Doc`  [EXTRACTED]
  fd-text-core/tests/test_news_versioning.py → fd-text-core/fd_text_core/ingest.py
- `test_merger_split_reports_article_and_doc_number()` --calls--> `EntityMerger`  [EXTRACTED]
  fd-text-core/tests/test_kg_lane_merge_wiring.py → fd-text-core/fd_text_core/kg_lane/merge.py
- `_register()` --uses--> `FacetError`  [INFERRED]
  fd-text-core/fd_text_core/server.py → fd-text-core/fd_text_core/querylib.py
- `GovSectionStrategy` --uses--> `SlidingWindow`  [INFERRED]
  fd-text-core/fd_text_core/verticals/gov/__init__.py → fd-text-core/fd_text_core/chunking.py
- `run_once()` --uses--> `Embedder`  [INFERRED]
  fd-text-core/fd_text_core/embed_worker.py → fd-text-core/fd_text_core/embedding.py

## Import Cycles
- None detected.

## Communities (95 total, 32 thin omitted)

### Community 0 - "cost.py"
Cohesion: 0.05
Nodes (22): classify_operation(), CostLedger, CostRecord, CostRecordingLLMClient, _delegate(), _EmbedderClient, extrapolate(), LedgerEmbedder (+14 more)

### Community 1 - "merge.py"
Cohesion: 0.06
Nodes (25): canonical_name(), EntityMerger, EntitySplit, inline_docno(), is_idempotent(), merge_key(), normalize(), plan_merge() (+17 more)

### Community 2 - "cjk_tsquery"
Cohesion: 0.06
Nodes (14): cjk_tokens(), cjk_tokens_text(), cjk_tsquery(), fold_t2s(), _prep(), _safe(), _t2s_map(), _alembic() (+6 more)

### Community 3 - "test_kg_tools.py"
Cohesion: 0.07
Nodes (17): _assert_fact_contract(), _call(), _edge(), fake_backend(), FakeBackend, runtime_tools(), test_backend_unavailable_reports_upstream(), test_invalid_as_of_rejected() (+9 more)

### Community 4 - "parse"
Cohesion: 0.07
Nodes (19): EffectiveInfo, parse(), test_amend_bill_host(), test_amend_modify_decision_links_corpus(), test_amend_version_paren(), test_effective_cn_year_form_only_no_date(), test_effective_cn_year_variants(), test_effective_docno_gzk() (+11 more)

### Community 5 - "eval.py"
Cohesion: 0.07
Nodes (18): BaselineClient, DualEvalRunner, EvalQuestion, EvalReport, EvalResult, load_questions(), McpBaselineClient, main() (+10 more)

### Community 6 - "conformance_gate.py"
Cohesion: 0.07
Nodes (31): BlockFormatError, decode_block(), encode_block(), get_block(), put_block(), roundtrip_ok(), _changed_files(), check_anchor() (+23 more)

### Community 7 - "test_gov_parser.py"
Cohesion: 0.06
Nodes (25): _parse(), test_all_templates_yield_core_fields(), test_body_has_no_site_chrome(), test_facets_and_presets_declared(), test_gonggao_type_not_misread_from_inner_title(), test_mnr_notice_addressee_absent_not_invented(), test_mnr_notice_attachments_registered(), test_mnr_notice_body_contains_closing() (+17 more)

### Community 8 - "statute_layer.py"
Cohesion: 0.09
Nodes (21): AmendInfo, target_node_for(), _day_iso(), _edge_uuid(), _ep_uuid(), _episode(), _episode_content(), _episode_valid_at() (+13 more)

### Community 9 - "lane.py"
Cohesion: 0.08
Nodes (16): KGVerticalConfig, build_extraction_instructions(), build_graphiti(), build_lane(), _CrossEncoderBase, _delegate_to(), _entity_type_models(), episode_kwargs() (+8 more)

### Community 10 - "connectors.py"
Cohesion: 0.08
Nodes (23): arxiv_search(), _ArxivThrottler, _classify_http_error(), ConnectorResult, federated_search(), _http_json(), openalex_citations(), openalex_references() (+15 more)

### Community 11 - "papers/harvest.py"
Cohesion: 0.07
Nodes (18): harvest_metadata(), HarvestError, looks_like_tex(), Paper, parse_authors(), _text(), Throttler, test_first_request_requires_date_window() (+10 more)

### Community 12 - "llm.py"
Cohesion: 0.06
Nodes (28): RecordingOpenAI, _any_parses(), build_llm_client(), _build_response_format(), _generate_response(), drop_incomplete_items(), _list_fields(), LLMSettings (+20 more)

### Community 13 - "screenplay/harvest.py"
Cohesion: 0.09
Nodes (15): _cn_to_int(), fetch_ctext(), fetch_gutenberg(), fetch_wikisource(), fetch_xikao(), HarvestError, html_to_text(), looks_copyrighted() (+7 more)

### Community 14 - "containers"
Cohesion: 0.06
Nodes (38): env, envFrom, image, imagePullPolicy, livenessProbe, name, ports, readinessProbe (+30 more)

### Community 15 - "runner.py"
Cohesion: 0.12
Nodes (14): embedder_from_env(), compute_fingerprint(), Dataset, dataset_path(), list_datasets(), load_all(), load_dataset(), _parse_graded() (+6 more)

### Community 16 - "pathlib"
Cohesion: 0.08
Nodes (20): _s3(), Doc, iter_structured(), load_documents(), Provenance, _facet_doc_type(), _facet_issuer(), _facet_period() (+12 more)

### Community 17 - "test_kg_lane_merge_wiring.py"
Cohesion: 0.11
Nodes (15): _FakeGraphiti, _gov_like_dict(), _raw_extraction(), _ScriptedLLM, test_alias_lexicon_accepts_valid_entries(), test_alias_lexicon_multiple_aliases_to_same_target_ok(), test_alias_lexicon_rejects_chains(), test_alias_lexicon_rejects_empty_or_non_string_entries() (+7 more)

### Community 18 - "querylib.py"
Cohesion: 0.13
Nodes (15): fetch_all(), EmbeddingUnavailable, require_triplet(), _cjk_enabled(), FacetError, _filters_sql(), hybrid_search(), keyword_search() (+7 more)

### Community 19 - "re"
Cohesion: 0.08
Nodes (16): Attachment, _clean_text(), _clean_title(), _closing_date(), _detect_doc_type(), _extract_labeled_values(), _find_body_container(), GovDoc (+8 more)

### Community 20 - "with"
Cohesion: 0.06
Nodes (34): runs-on, steps, concurrency, cancel-in-progress, group, env, DOCKERFILE, IMAGE_NAME (+26 more)

### Community 21 - "query.py"
Cohesion: 0.10
Nodes (9): _as_utc(), EdgeRecord, fact_valid_at(), FactView, parse_dt(), QueryBackend, _edge(), test_fact_valid_at_accepts_naive_as_of() (+1 more)

### Community 22 - "registry.py"
Cohesion: 0.13
Nodes (9): Preset, GovParser, bootstrap_defaults(), _default_gov_facets(), _default_paper_facets(), _default_screenplay_facets(), register(), replace() (+1 more)

### Community 24 - "test_ingest_contract.py"
Cohesion: 0.10
Nodes (12): FakeS3, _Body, _insert_doc(), _jsonl(), _row(), test_iter_structured_folds_multi_run_latest_wins(), test_iter_structured_limit_counts_after_folding(), test_iter_structured_takes_explicit_bucket() (+4 more)

### Community 25 - "test_kg_lane_kernel.py"
Cohesion: 0.07
Nodes (23): _close_unterminated(), unwrap_schema_envelope(), _embedder(), _FakeEmbeddings, test_close_unterminated_ignores_brackets_inside_strings(), test_close_unterminated_leaves_valid_json_untouched(), test_close_unterminated_string_backslash_pairs_stay_intact(), test_close_unterminated_string_cut_right_after_backslash() (+15 more)

### Community 26 - "test_screenplay.py"
Cohesion: 0.11
Nodes (16): _clean(), Line, parse_play(), ParsedPlay, Scene, _strip_role_tail(), _strip_stage(), test_both_forms_share_one_contract() (+8 more)

### Community 28 - "test_tool_annotations.py"
Cohesion: 0.14
Nodes (5): _has_connector(), test_annotations_are_copies(), test_local_retrieval_is_closed_world(), test_open_world_matches_real_data_source(), test_unknown_tool_raises_instead_of_defaulting()

### Community 29 - "test_kg_lane_config.py"
Cohesion: 0.19
Nodes (16): from_dict(), test_admission_blocks_verticals_with_external_graph(), test_admission_requires_evidence(), test_duplicate_entity_types_rejected(), test_entity_type_models_are_attributeless(), test_entity_type_needs_meaningful_description(), test_episode_kwargs_has_no_uuid(), test_granularity_chunking_accepted_with_evidence() (+8 more)

### Community 30 - "test_keyword_ranking.py"
Cohesion: 0.11
Nodes (13): _exact_first(), capture(), _Capture, _fused(), _order_clause(), test_cjk_branch_exact_match_factor_precedes_rank(), test_exact_first_case_insensitive(), test_exact_first_noop_when_no_candidate_contains_query() (+5 more)

### Community 31 - "Embedder"
Cohesion: 0.17
Nodes (10): execute(), Embedder, EmbedResult, provider_from_env(), main(), step1_verify_channel(), step2_clear_vectors(), step3_rebuild_anchor() (+2 more)

### Community 32 - "steps"
Cohesion: 0.08
Nodes (24): runs-on, steps, FDTEXT_DATABASE_URL, FDTEXT_S3_ACCESS_KEY, FDTEXT_S3_BUCKET, FDTEXT_S3_ENDPOINT, FDTEXT_S3_SECRET_KEY, jobs (+16 more)

### Community 33 - "kg_lane_run.py"
Cohesion: 0.10
Nodes (15): build_corpus_index(), KGLane, settings_from_env(), need(), load_episodes(), main(), drop_graph(), load_episodes() (+7 more)

### Community 34 - "embed_worker.py"
Cohesion: 0.12
Nodes (17): _cjk_enabled(), claim_batch(), cosine(), _document_text(), ensure_chunks(), GateFailure, _parse_vector(), reclaim_stale() (+9 more)

### Community 35 - "Grade"
Cohesion: 0.14
Nodes (10): Grade, Query, _log2(), ndcg_at_k(), test_binary_query_equivalent_to_single_grade2(), test_graded_query_max_grade_ids_allows_ties(), test_ndcg_ideal_order_is_one(), test_ndcg_missing_expected_is_zero() (+2 more)

### Community 36 - "test_eval.py"
Cohesion: 0.13
Nodes (13): _report(), test_compare_detects_regression_when_fingerprints_match(), test_compare_no_baseline(), test_compare_ok_when_same_or_better(), test_compare_refuses_when_config_changed(), test_compare_refuses_when_dataset_changed(), test_compare_refuses_when_k_changed(), test_eval_module_does_not_import_query_internals() (+5 more)

### Community 37 - "db.py"
Cohesion: 0.11
Nodes (7): ConfigError, _require(), Settings, cursor(), get_pool(), _settings(), UpstreamUnavailable

### Community 38 - "KGLaneQuery"
Cohesion: 0.18
Nodes (11): KGLaneQuery, NodeRecord, _edge_t(), _graph(), test_as_of_filters_expired_fact(), test_walk_collects_hops(), test_walk_honours_fanout_cap(), test_walk_never_returns_empty_due_to_full_suppression() (+3 more)

### Community 39 - "test_news_versioning.py"
Cohesion: 0.22
Nodes (6): current_document_ids(), test_nul_in_body_ingests_after_entry_cleanup(), test_three_content_changes_supersede_in_order(), doc(), textcore_env(), _url()

### Community 40 - "spec"
Cohesion: 0.10
Nodes (19): apiVersion, prune, selfHeal, namespace, server, kind, metadata, name (+11 more)

### Community 41 - "build_statute_graph"
Cohesion: 0.16
Nodes (7): build_statute_graph(), _utc_iso(), load_fixture(), test_build_plan_idempotent(), test_docno_contradiction_blocked_and_recorded(), test_docno_identity_same_title_different_docno(), test_uuid_independent_of_build_clock()

### Community 42 - "kg_lane/config.py"
Cohesion: 0.11
Nodes (13): AdmissionSpec, clear(), ConfigError, CorpusSpec, EntityTypeSpec, get(), GranularitySpec, list_verticals() (+5 more)

### Community 43 - "test_kg_statute_layer.py"
Cohesion: 0.11
Nodes (20): _edges(), _node_by_name(), _plan(), rec(), test_amend_edges(), test_audit_regexes_verbatim(), test_decision_class_status_not_residual(), test_edge_uuid_and_datetime_fields_are_read_model_safe() (+12 more)

### Community 44 - "chunk_text"
Cohesion: 0.13
Nodes (11): Chunk, chunk_text(), verify_lossless(), GovSectionStrategy, test_chunking_long_doc_without_headings_falls_back_to_sliding_window(), test_chunking_lossless_on_all_templates(), test_chunking_preserves_section_path(), test_chunking_short_doc_single_block() (+3 more)

### Community 45 - "Any"
Cohesion: 0.14
Nodes (6): degraded_embedding_report(), StatuteEdge, StatuteGraphPlan, StatuteGraphWriter, test_degraded_embedding_report_counts_nodes(), test_writer_requires_connect()

### Community 46 - "server.py"
Cohesion: 0.08
Nodes (28): annotations_for(), _backend_or_error(), _build_query(), _decorate_facts(), _ok(), register_kg_tools(), kg_facts_as_of(), kg_search_facts() (+20 more)

### Community 47 - "selfcheck_surface.py"
Cohesion: 0.18
Nodes (10): check_annotations(), check_output_structure(), consumer_tools(), expected_tools(), fail(), load_manifest(), main(), manifest_tools() (+2 more)

### Community 48 - "test_pure.py"
Cohesion: 0.10
Nodes (5): test_coldstore_default_read_is_not_truncated(), test_eval_all_partitions_labeled_domains_by_registry(), test_lossless_invariant(), test_short_document_stores_content_and_blocks_decompose(), test_tsv_config_falls_back_for_unregistered_domain()

### Community 49 - "scale_smoke.py"
Cohesion: 0.13
Nodes (10): FakeEmbedder, cleanup(), counts(), drain_embed(), ensure_partition(), ensure_vertical(), main(), _require_env() (+2 more)

### Community 50 - "DatasetError"
Cohesion: 0.17
Nodes (11): DatasetError, _good_payload(), test_dataset_min_queries_enforced(), test_dataset_reports_every_bad_query_with_position(), test_dataset_requires_how_targets_chosen(), test_dataset_requires_judgment_strength(), test_graded_dataset_requires_grade2(), test_illegal_domain_rejected() (+3 more)

### Community 52 - "kg_statute_repair_names.py"
Cohesion: 0.06
Nodes (27): build_embedder(), _check(), create(), create_batch(), _call(), _call(), _kwargs(), EmbeddingDimensionError (+19 more)

### Community 53 - "asyncio"
Cohesion: 0.19
Nodes (6): _call(), test_error_body_still_carries_code_and_message(), test_every_emitted_code_maps_into_the_four_classes(), test_not_supported_is_an_error_result_not_a_success(), test_successful_call_is_not_an_error_result(), runtime()

### Community 54 - "papers/parser.py"
Cohesion: 0.17
Nodes (7): PaperParser, detex(), iter_sections(), parse_fulltext(), parse_grobid_tei(), parse_latex(), ParsedPaper

### Community 55 - "MergingLLMClient"
Cohesion: 0.20
Nodes (4): _LLMClientBase, MergingLLMClient, test_merging_client_forwards_all_kwargs_to_inner(), test_merging_client_rewrites_only_extraction_payloads()

### Community 56 - "parse_doc_numbers"
Cohesion: 0.13
Nodes (8): cn2int(), DocNumberInfo, _order_token(), parse_doc_numbers(), test_cn2int(), test_docno_issuer_duplicate_prefix_normalized(), test_docno_order_number_variants(), test_docno_title_inline_kept()

### Community 58 - "load"
Cohesion: 0.22
Nodes (6): load(), load_shipped_verticals(), test_shipped_vertical_configs_load_and_pass_admission(), _legal_dict(), test_alias_maps_whole_name_only_not_substring(), test_shipped_legal_config_has_alias_lexicon_gov_has_none()

### Community 61 - "list_verticals"
Cohesion: 0.15
Nodes (11): NewsParser, clean_news_text(), parse_news(), list_verticals(), load_verticals(), test_clean_text_preserves_content(), test_news_is_a_registered_vertical(), test_parser_does_not_invent_a_title() (+3 more)

### Community 62 - "EvalReport"
Cohesion: 0.21
Nodes (4): compare_with_previous(), EvalReport, test_saturated_detection(), test_variant_isolation_in_compare()

### Community 63 - "json"
Cohesion: 0.33
Nodes (3): _default_post(), main(), test_manifest_annotations_match_runtime()

### Community 66 - "attach_name_embeddings"
Cohesion: 0.24
Nodes (5): attach_name_embeddings(), FakeEmbedder, test_attach_name_embeddings_batch_failure_degrades_and_reports(), test_attach_name_embeddings_success(), test_attach_name_embeddings_vector_count_mismatch_degrades()

### Community 67 - "backfill_gov_facets.py"
Cohesion: 0.53
Nodes (4): facet_doc_type(), facet_issuer(), facet_period(), main()

### Community 69 - "verify_vector_dual_read.py"
Cohesion: 0.31
Nodes (6): collect(), compare(), _conn(), fetch_topk(), main(), self_channels()

### Community 71 - "fetch_one"
Cohesion: 0.36
Nodes (3): fetch_one(), latest_previous(), store_run()

### Community 72 - "test_cli_seed_order_sql_params"
Cohesion: 0.12
Nodes (7): _clear_embed_env(), _load_cli(), test_cli_dry_run_writes_reports(), test_cli_no_embed_marks_skipped(), test_cli_seed_order_sql_params(), test_embedding_settings_from_env(), test_fetch_sql_is_read_only()

## Knowledge Gaps
- **1 isolated node(s):** `fd-text-core`
  These have ≤1 connection - possible missing edges. (Counts symbols only; 823 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **32 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `cjk_tsquery()` connect `cjk_tsquery` to `querylib.py`?**
  _High betweenness centrality (0.025) - this node is a cross-community bridge._
- **What connects `fd-text-core` to the rest of the system?**
  _1 weakly-connected nodes found - possible missing edges._
- **Should `cost.py` be split into smaller, more focused modules?**
  _Cohesion score 0.05357142857142857 - nodes in this community are weakly interconnected._
- **Why does `GraphitiQueryBackend` connect `GraphitiQueryBackend` to `kg_lane_run.py`, `lane.py`, `server.py`, `kg_statute_repair_names.py`, `query.py`?**
  _High betweenness centrality (0.017) - this node is a cross-community bridge._
- **Should `merge.py` be split into smaller, more focused modules?**
  _Cohesion score 0.05925925925925926 - nodes in this community are weakly interconnected._
- **Why does `parse_gov_doc()` connect `re` to `registry.py`, `test_gov_parser.py`?**
  _High betweenness centrality (0.016) - this node is a cross-community bridge._
- **Should `cjk_tsquery` be split into smaller, more focused modules?**
  _Cohesion score 0.060496067755595885 - nodes in this community are weakly interconnected._
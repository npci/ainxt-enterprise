#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
One-shot idempotent bootstrap: register an LLM provider (and optionally one
model) in the llm_providers/llm_models tables from what install.sh already
collected, so the admin "LLM Providers" screen shows exactly what was
configured during install — without requiring the admin to re-enter it.

Reads the actual secret/base-url values from environment variables rather
than accepting them as CLI arguments, so a plaintext API key never appears in
`ps` output or shell history. install.sh invokes this once per configured
provider, either via `docker exec ainxt-gateway ...` (the container already
has ANTHROPIC_API_KEY/OPENAI_API_KEY/GEMINI_API_KEY/OLLAMA_URL from
docker-compose's environment block) or via the local venv after
export_env_for_host() has sourced .env into the shell.

Usage:
    python db/bootstrap_llm_providers.py --family anthropic --slug anthropic-default \\
        --name "Anthropic (from install)" --key-env-var ANTHROPIC_API_KEY

    python db/bootstrap_llm_providers.py --family ollama --slug ollama-default \\
        --name "Ollama (local)" --base-url-env-var LOCAL_LLM_BASE_URL --seed-model llama3.2

Safe to re-run: an existing provider with the same slug is left untouched
(an admin may have already edited it in the UI), and a model row is only
added if it doesn't already exist.
"""
import argparse
import os
import sys

_VALID_FAMILIES = ("anthropic", "openai", "gemini", "openai_compatible", "ollama")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--family", required=True, choices=_VALID_FAMILIES)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--key-env-var", default=None, help="env var holding the API key, e.g. ANTHROPIC_API_KEY")
    parser.add_argument("--base-url-env-var", default=None, help="env var holding the base URL, e.g. LOCAL_LLM_BASE_URL")
    parser.add_argument("--seed-model", default=None, help="model_id to register immediately (e.g. after an Ollama pull)")
    args = parser.parse_args()

    api_key = os.getenv(args.key_env_var) if args.key_env_var else None
    base_url = os.getenv(args.base_url_env_var) if args.base_url_env_var else None

    if args.family != "ollama" and not api_key:
        print(f"  = skipping '{args.slug}': {args.key_env_var} is not set")
        return 0

    from db.database import SessionLocal
    from db.models import LLMProvider, LLMModel
    from core.llm_provider_registry import credential_name_for_slug, invalidate_cache
    from store.credential_vault import create_credential, get_credential

    db = SessionLocal()
    try:
        provider = db.query(LLMProvider).filter_by(slug=args.slug).first()
        if provider:
            print(f"  = provider '{args.slug}' already exists — leaving it as configured in the admin screen")
        else:
            credential_id = None
            if api_key:
                cred_name = credential_name_for_slug(args.slug)
                cred = get_credential(cred_name)
                if not cred:
                    cred = create_credential(
                        name=cred_name, value=api_key, category="api_key",
                        description=f"Seeded by install.sh for provider '{args.slug}'",
                    )
                credential_id = cred["id"]

            provider = LLMProvider(
                name=args.name, slug=args.slug, family=args.family,
                base_url=base_url, credential_id=credential_id,
                enabled=True, extra_config={}, created_by="install.sh",
            )
            db.add(provider)
            db.flush()
            print(f"  + registered provider '{args.slug}' (family={args.family})")

        if args.seed_model:
            exists = (
                db.query(LLMModel)
                .filter_by(provider_id=provider.id, model_id=args.seed_model)
                .first()
            )
            if not exists:
                db.add(LLMModel(
                    provider_id=provider.id, model_id=args.seed_model, display_name=args.seed_model,
                    capabilities={}, enabled=True, source="seed", created_by="install.sh",
                ))
                print(f"  + registered model '{args.seed_model}' under '{args.slug}'")

        # Auto-discover this provider's real model catalog — a fresh install
        # should be immediately usable in chat without the admin having to
        # open the "LLM Providers" screen and click "Sync" by hand first.
        # Cloud families (anthropic/openai/gemini) only do this once, when the
        # provider has no models yet at all — never overwrites/duplicates an
        # admin's already-curated list on a re-run of install.sh. Ollama's
        # discovery only ever ADDS models it finds already pulled that aren't
        # registered yet (never removes/overwrites anything), so it's safe to
        # attempt every run.
        existing_model_ids = {
            m.model_id for m in db.query(LLMModel).filter_by(provider_id=provider.id).all()
        }
        if args.family == "ollama" or not existing_model_ids:
            try:
                from routers.llm_provider_admin_router import _discover_models
                discovered = _discover_models(provider)
                added = 0
                for d in discovered:
                    if d["model_id"] in existing_model_ids:
                        continue
                    db.add(LLMModel(
                        provider_id=provider.id, model_id=d["model_id"], display_name=d["display_name"],
                        capabilities=d["capabilities"], enabled=True, source="seed", created_by="install.sh",
                    ))
                    existing_model_ids.add(d["model_id"])
                    added += 1
                if added:
                    print(f"  + auto-synced {added} model(s) for '{args.slug}'")
            except Exception as exc:
                print(f"  = could not auto-sync models for '{args.slug}' — sync manually in the admin screen ({exc})")

        # A fresh install should come up with a real default model already
        # picked, not an empty one the admin has to set by hand in the UI —
        # no-op if some other provider/model already has the flag.
        from core.llm_provider_registry import ensure_default_model
        if ensure_default_model(db):
            print(f"  + set platform default model from '{args.slug}'")

        db.commit()
        invalidate_cache()
        return 0
    except Exception as exc:
        db.rollback()
        print(f"  ! bootstrap_llm_providers failed for '{args.slug}': {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())

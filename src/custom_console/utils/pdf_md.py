"""
pdf_to_summary.py
Pipeline : PDF -> Markdown (via pymupdf4llm) -> résumé via Ollama local.

Installation :
    pip install pymupdf4llm ollama

Prérequis :
    - Ollama installé et lancé (`ollama serve`)
    - Un modèle téléchargé, ex : `ollama pull llama3.1`

Utilisation :
    python pdf_to_summary.py mon_document.pdf --model llama3.1
"""

import argparse
from pathlib import Path

import ollama
import pymupdf4llm


def pdf_to_markdown(pdf_path: Path, output_dir: Path) -> Path:
    """Convertit un PDF en Markdown avec pymupdf4llm et sauvegarde le résultat."""
    md_text = pymupdf4llm.to_markdown(str(pdf_path))
    md_path = output_dir / f"{pdf_path.stem}.md"
    md_path.write_text(md_text, encoding="utf-8")
    return md_path


def chunk_text(text: str, max_chars: int = 12000) -> list[str]:
    """Découpe le texte en morceaux (par paragraphes) pour respecter le contexte du modèle."""
    chunks = []
    current = ""
    for paragraph in text.split("\n\n"):
        if len(current) + len(paragraph) > max_chars and current:
            chunks.append(current)
            current = paragraph
        else:
            current += ("\n\n" if current else "") + paragraph
    if current:
        chunks.append(current)
    return chunks


def summarize_chunk(chunk: str, model: str) -> str:
    prompt = (
        "Tu es un assistant qui résume des documents scientifiques ou techniques. "
        "Résume le texte Markdown suivant en français, de façon claire et concise. "
        "IMPORTANT : conserve intégralement toutes les expressions mathématiques "
        "au format LaTeX (entre $ ou $$), ne les simplifie pas, ne les traduis pas "
        "en texte et ne les paraphrase pas.\n\n"
        f"Texte à résumer :\n{chunk}"
    )
    response = ollama.chat(
        model=model, messages=[{"role": "user", "content": prompt}], think=False
    )
    print(response.eval_duration)
    eval_dur = response.eval_duration if response.eval_duration else 1e9
    print(
        f"Tokens : prompt {response.prompt_eval_count}, answer {response.eval_count}, speed {response.eval_count / eval_dur * 1e9}."
    )
    return response["message"]["content"]


def summarize_markdown(md_text: str, model: str) -> str:
    chunks = chunk_text(md_text)

    if len(chunks) == 1:
        return summarize_chunk(chunks[0], model)

    print(f"Document découpé en {len(chunks)} morceaux, résumé de chacun...")
    partial_summaries = [summarize_chunk(c, model) for c in chunks]
    combined = "\n\n---\n\n".join(partial_summaries)

    final_prompt = (
        "Voici plusieurs résumés partiels d'un même document, dans l'ordre. "
        "Fusionne-les en un résumé unique, cohérent et non redondant, en conservant "
        "toutes les expressions mathématiques en LaTeX telles quelles.\n\n" + combined
    )
    response = ollama.chat(
        model=model, messages=[{"role": "user", "content": final_prompt}], think=False
    )
    print(response.eval_duration)
    eval_dur = response.eval_duration if response.eval_duration else 1e9
    print(
        f"Tokens : prompt {response.prompt_eval_count}, answer {response.eval_count}, speed {response.eval_count / eval_dur * 1e9}."
    )
    return response["message"]["content"]


def main():
    parser = argparse.ArgumentParser(
        description="Résume un PDF via Ollama en local, en conservant les formules mathématiques."
    )
    parser.add_argument("pdf", type=Path, help="Chemin vers le PDF à résumer")
    parser.add_argument(
        "--model",
        default="gpt-oss:20b-cloud",
        help="Nom du modèle Ollama (ex: llama3.1, mistral, qwen2.5)",
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        default=Path("./pdf_pipeline_output"),
        help="Dossier de travail",
    )
    args = parser.parse_args()

    args.workdir.mkdir(exist_ok=True)

    print("Conversion du PDF en Markdown (pymupdf4llm)...")
    md_path = pdf_to_markdown(args.pdf, args.workdir)
    md_text = md_path.read_text(encoding="utf-8")
    print(f"Markdown généré : {md_path}")

    print(f"Résumé en cours avec le modèle Ollama '{args.model}'...")
    summary = summarize_markdown(md_text, args.model)

    summary_path = args.workdir / f"{args.pdf.stem}_resume.md"
    summary_path.write_text(summary, encoding="utf-8")
    print(f"Résumé enregistré dans : {summary_path}")


if __name__ == "__main__":
    main()

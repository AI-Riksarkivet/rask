"""Diff two directories of ALTO XML files (same filenames) to check text-equivalence.

Reports: total files, fully identical, byte-differences (any), text-content differences
(extracted from <String CONTENT="..."/>), and shows up to N examples of text diffs.

Usage: uv run python diff_xml.py <ref_dir> <test_dir> [--show-n 10]
"""

import argparse
import re
from pathlib import Path


CONTENT_RE = re.compile(r'<String [^>]*CONTENT="([^"]*)"')


def extract_texts(xml: str) -> list[str]:
    return CONTENT_RE.findall(xml)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ref_dir", type=Path)
    ap.add_argument("test_dir", type=Path)
    ap.add_argument("--show-n", type=int, default=10)
    args = ap.parse_args()

    ref_files = sorted(args.ref_dir.glob("*.xml"))
    test_files = sorted(args.test_dir.glob("*.xml"))

    ref_names = {p.name for p in ref_files}
    test_names = {p.name for p in test_files}

    missing = ref_names - test_names
    extra = test_names - ref_names
    common = sorted(ref_names & test_names)

    print(f"Reference: {args.ref_dir} ({len(ref_files)} files)")
    print(f"Test:      {args.test_dir} ({len(test_files)} files)")
    if missing:
        print(f"Missing in test ({len(missing)}): {sorted(missing)[:5]}...")
    if extra:
        print(f"Extra in test ({len(extra)}): {sorted(extra)[:5]}...")

    n_identical_bytes = 0
    n_identical_text = 0
    n_text_differs = 0
    total_ref_lines = 0
    total_diff_lines = 0
    examples: list[tuple[str, list[str], list[str]]] = []

    for name in common:
        ref = (args.ref_dir / name).read_text()
        test = (args.test_dir / name).read_text()
        if ref == test:
            n_identical_bytes += 1
            n_identical_text += 1
            total_ref_lines += len(extract_texts(ref))
            continue
        ref_texts = extract_texts(ref)
        test_texts = extract_texts(test)
        total_ref_lines += len(ref_texts)
        if ref_texts == test_texts:
            n_identical_text += 1
        else:
            n_text_differs += 1
            diffs = [(r, t) for r, t in zip(ref_texts, test_texts, strict=False) if r != t]
            diffs += [(r, "<MISSING>") for r in ref_texts[len(test_texts) :]]
            diffs += [("<MISSING>", t) for t in test_texts[len(ref_texts) :]]
            total_diff_lines += len(diffs)
            if len(examples) < args.show_n:
                examples.append((name, [d[0] for d in diffs[:3]], [d[1] for d in diffs[:3]]))

    print("\n=== Summary ===")
    print(f"Common files:            {len(common)}")
    print(f"Byte-identical:          {n_identical_bytes} ({n_identical_bytes / max(len(common), 1) * 100:.1f}%)")
    print(f"Text-identical:          {n_identical_text} ({n_identical_text / max(len(common), 1) * 100:.1f}%)")
    print(f"Text differs:            {n_text_differs}")
    print(f"Total lines (ref):       {total_ref_lines}")
    print(f"Lines with text diff:    {total_diff_lines} ({total_diff_lines / max(total_ref_lines, 1) * 100:.3f}%)")

    if examples:
        print(f"\n=== First {len(examples)} examples of text diffs ===")
        for name, ref_samples, test_samples in examples:
            print(f"\n{name}:")
            for r, t in zip(ref_samples, test_samples, strict=False):
                print(f"  REF:  {r!r}")
                print(f"  TEST: {t!r}")


if __name__ == "__main__":
    main()

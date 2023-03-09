import argparse
import dataclasses
import itertools
import json
import io
import os
import glob
from pathlib import Path
from typing import List

from meeteval.io.ctm import CTMGroup
from meeteval.io.stm import STM
from meeteval.wer.wer import (
    cp_word_error_rate,
    orc_word_error_rate,
    mimo_word_error_rate,
    combine_error_rates,
    ErrorRate,
    CPErrorRate,
)
import sys


def _dump(obj, path: 'Path | str', default_suffix='.json'):
    """
    Dumps the `obj` to `path`. Parses the suffix to find the file type.
    When a suffix is missing, use json.
    """
    path = Path(path)
    if path.stem == '-':
        from contextlib import nullcontext
        p = nullcontext(sys.stdout)
    else:
        try:
            p = path.open('w')
        except FileNotFoundError as e:
            raise FileNotFoundError(
                f'Couldn\'t open the output file. Consider explicitly setting '
                f'the output files, especially when piping into this tool.'
            ) from e
    with p as fd:
        suffix = path.suffix
        if suffix == '':
            suffix = default_suffix
        if suffix == '.json':
            json.dump(obj, fd, indent=2, sort_keys=False)
        elif suffix == '.yaml':
            import yaml
            yaml.dump(obj, fd, sort_keys=False)
        else:
            raise NotImplemented(f'Unknown file ext: {suffix}')

    if path.stem != '-':
        print(f'Wrote: {path}', file=sys.stderr)


def _load(path: Path):
    with path.open('r') as fd:
        if path.suffix == '.json':
            return json.load(fd)
        elif path.suffix == '.yaml':
            import yaml
            return yaml.load(fd)
        else:
            raise NotImplemented(f'Unknown file ext: {path.suffix}')


def _load_reference(reference: 'Path | List[Path]'):
    """Loads a reference transcription file. Currently only STM supported"""
    return STM.load(reference)


def _load_hypothesis(hypothesis: List[Path]):
    """Loads the hypothesis. Supports one STM file or multiple CTM files
    (one per channel)"""
    if len(hypothesis) > 1:
        # We have multiple, only supported for ctm files
        suffix = {h.suffixes[-1] for h in hypothesis}
        assert len(suffix) == 1, suffix
        filename = suffix.pop()
    else:
        hypothesis = hypothesis[0]
        filename = str(hypothesis)

    if filename.endswith('.ctm'):
        if isinstance(hypothesis, list):
            return CTMGroup.load(hypothesis)
        else:
            return CTMGroup.load([hypothesis])
    elif filename.endswith('.stm'):
        return STM.load(hypothesis)
    elif filename.startswith('/dev/fd/') or filename.startswith('/proc/self/fd/'):
        # This is a pipe, i.e. python -m ... <(cat ...)
        # For now, assume it is an STM file
        return STM.load(hypothesis)
    else:
        raise RuntimeError(hypothesis, filename)


def _load_texts(reference_paths: List[str], hypothesis_paths: List[str]):
    """Load and validate reference and hypothesis texts.

    Validation checks that reference and hypothesis have the same example IDs.
    """

    # Normalize and glob (for backwards compatibility) the path input
    def _glob(pathname):
        match = list(glob.glob(pathname))
        # Forward pathname if not matched to get the correct error message
        return match or [pathname]

    reference_paths = [Path(file) for r in reference_paths for file in _glob(r)]
    hypothesis_paths = [Path(file) for h in hypothesis_paths for file in _glob(h)]

    # Load input files
    reference = _load_reference(reference_paths)

    # Hypothesis can be an STM file or a collection of  CTM files. Detect
    # which one we have and load it
    hypothesis = _load_hypothesis(hypothesis_paths)

    # Group by example IDs
    reference = reference.grouped_by_filename()
    hypothesis = hypothesis.grouped_by_filename()

    # Check that the input is valid
    if reference.keys() != hypothesis.keys():
        raise RuntimeError(
            'Keys of reference and hypothesis differ\n'
            f'hypothesis - reference: e.g. {list(set(hypothesis.keys()) - set(reference.keys()))[:5]}\n'
            f'reference - hypothesis: e.g. {list(set(reference.keys()) - set(hypothesis.keys()))[:5]}'
        )

    return reference, reference_paths, hypothesis, hypothesis_paths


def _get_parent_stem(hypothesis_paths: List[Path]):
    hypothesis_paths = [p.resolve() for p in hypothesis_paths]

    if len(hypothesis_paths) == 1:
        parent, stem = hypothesis_paths[0].parent, hypothesis_paths[0].stem
    else:
        # Find the common parent and "stem" when we have multiple files
        parent = os.path.commonpath(hypothesis_paths)

        stems = [p.stem for p in hypothesis_paths]
        prefix = os.path.commonprefix(stems)
        postfix = os.path.commonprefix([s[::-1] for s in stems])[::-1]

        stem = f'{prefix}{postfix}'

    return parent, stem


def _save_results(
        per_reco,
        hypothesis_paths: List[Path],
        per_reco_out: str,
        average_out: str,
):
    """Saves the results.
    """
    parent, stem = _get_parent_stem(hypothesis_paths)

    # Save details
    _dump({
        example_id: dataclasses.asdict(error_rate)
        for example_id, error_rate in per_reco.items()
    }, per_reco_out.format(parent=f'{parent}/', stem=stem))

    # Compute and save average
    average = combine_error_rates(*per_reco.values())
    _dump(
        dataclasses.asdict(average),
        average_out.format(parent=f'{parent}/', stem=stem),
    )


# Define argument parser and commands
parser = argparse.ArgumentParser()
parser.add_argument('--version', action='store_true', help='Show version')
commands = parser.add_subparsers(title='Subcommands')


def command(fn):
    command_parser = commands.add_parser(
        fn.__name__,
        add_help=False,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        help=fn.__doc__,
    )
    command_parser.add_argument(
        '--help', help='show this help message and exit',
        action='help',
        default=argparse.SUPPRESS,
    )
    # Get arguments from signature
    import inspect
    parameters = inspect.signature(fn).parameters

    for name, p in parameters.items():
        if name == 'reference':
            command_parser.add_argument(
                '-r', '--reference',
                help='Reference file(s) in STM or CTM format',
                nargs='+', action='extend',
            )
        elif name == 'hypothesis':
            command_parser.add_argument(
                '-h', '--hypothesis',
                help='Hypothesis file(s) in STM or CTM format',
                nargs='+', action='extend',
            )
        elif name == 'average_out':
            command_parser.add_argument(
                '--average-out',
                help='Output file for the average file. {stem} is replaced '
                     'with the stem of the (first) hypothesis file. '
                     '"-" is interpreted as stdout. For example: "-.yaml" '
                     'prints to stdout in yaml format.'
            )
        elif name == 'per_reco_out':
            command_parser.add_argument(
                '--per-reco-out',
                help='Output file for the per_reco file. {stem} is replaced '
                     'with the stem of the (first) hypothesis file. '
                     '"-" is interpreted as stdout. For example: "-.yaml" '
                     'prints to stdout in yaml format.'
            )
        elif name == 'out':
            command_parser.add_argument(
                '-o', '--out',
                required=False, default='-',
            )
        elif name == 'files':
            command_parser.add_argument('files', nargs='+')
        else:
            raise AssertionError("Error in command definition")

    # Get defaults from signature
    command_parser.set_defaults(
        func=fn,
        **{
            name: p.default
            for name, p in parameters.items()
            if p.default is not inspect.Parameter.empty
        }
    )
    return fn


@command
def orcwer(
        reference, hypothesis,
        average_out='{parent}/{stem}_orcwer.json',
        per_reco_out='{parent}/{stem}_orcwer_per_reco.json',
):
    """Computes the Optimal Reference Combination Word Error Rate (ORC WER)"""
    reference, _, hypothesis, hypothesis_paths = _load_texts(reference, hypothesis)
    _save_results({
        example_id: orc_word_error_rate(
            reference=reference[example_id].utterance_transcripts(),
            hypothesis={
                k: h.merged_transcripts()
                for k, h in hypothesis[example_id].grouped_by_speaker_id().items()
            },
        )
        for example_id in reference.keys()
    }, hypothesis_paths, per_reco_out, average_out)


@command
def cpwer(
        reference, hypothesis,
        average_out='{parent}/{stem}_cpwer.json',
        per_reco_out='{parent}/{stem}_cpwer_per_reco.json',
):
    """Computes the Concatenated minimum-Permutation Word Error Rate '
         '(cpWER)"""
    reference, _, hypothesis, hypothesis_paths = _load_texts(reference, hypothesis)
    _save_results({
        example_id: cp_word_error_rate(
            reference={
                k: r.merged_transcripts()
                for k, r in reference[example_id].grouped_by_speaker_id().items()
            },
            hypothesis={
                k: h.merged_transcripts()
                for k, h in hypothesis[example_id].grouped_by_speaker_id().items()
            },
        )
        for example_id in reference.keys()
    }, hypothesis_paths, per_reco_out, average_out)


@command
def mimower(
        reference, hypothesis,
        average_out='{parent}/{stem}_mimower.json',
        per_reco_out='{parent}/{stem}_mimower_per_reco.json',
):
    """Computes the MIMO WER"""
    reference, _, hypothesis, hypothesis_paths = _load_texts(reference, hypothesis)
    _save_results({
        example_id: mimo_word_error_rate(
            reference={
                k: r.utterance_transcripts()
                for k, r in reference[example_id].grouped_by_speaker_id().items()
            },
            hypothesis={
                k: h.merged_transcripts()
                for k, h in hypothesis[example_id].grouped_by_speaker_id().items()
            },
        )
        for example_id in reference.keys()
    }, hypothesis_paths, per_reco_out, average_out)


def _merge(
        files: List[str],
        out: str = None,
        average: bool = None
):
    # Load input files
    files = [Path(f) for f in files]
    data = [_load(f) for f in files]

    import meeteval
    ers = []

    for d in data:
        if 'errors' in d:  # Average file
            assert average is not False, average
            average = True  # A single average file forces to do an average
            ers.append([None, ErrorRate.from_dict(d)])
        else:
            for k, v in d.items():  # Details file
                if 'errors' in v:
                    ers.append([k, ErrorRate.from_dict(v)])

    if average:
        er = meeteval.wer.combine_error_rates(*[er for _, er in ers])
        out_data = dataclasses.asdict(er)
    else:
        out_data = {
            k: dataclasses.asdict(er)
            for k, er in ers
        }
        assert len(out_data) == len(ers), (len(out_data), len(ers), 'Duplicate filenames')

    _dump(out_data, Path(out), default_suffix=files[0].suffix)


@command
def merge(files, out):
    """Merges multiple (per-reco or averaged) files"""
    return _merge(files, out, average=None)


@command
def average(files, out):
    """Computes the average over one or multiple per-reco files"""
    return _merge(files, out, average=True)


def cli():
    # Parse arguments and find command to execute
    args = parser.parse_args()

    if hasattr(args, 'func'):
        kwargs = vars(args)
        fn = args.func
        # Pop also removes from args namespace
        kwargs.pop('func')
        kwargs.pop('version')
        return fn(**kwargs)

    if getattr(args, 'version', False):
        from meeteval import __version__
        print(__version__)
        return

    parser.print_help()


if __name__ == '__main__':
    cli()

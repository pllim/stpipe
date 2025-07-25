"""
Various utilities to handle running Steps from the commandline.
"""

import io
import logging
import os
import os.path
import textwrap

from . import config_parser, log, utilities
from .step import Step, get_disable_crds_steppars

built_in_configuration_parameters = [
    "debug",
    "logcfg",
    "verbose",
]

logger = logging.getLogger(__name__)


def _print_important_message(header, message, no_wrap=None):
    print("-" * 70)
    print(textwrap.fill(header))
    print(
        textwrap.fill(
            message,
            initial_indent="    ",
            subsequent_indent="    ",
        )
    )
    if no_wrap:
        print(no_wrap)
    print("-" * 70)


def _get_config_and_class(identifier):
    """
    Given a file path to a config file or Python module path, return a
    Step class and a configuration object.
    """
    if os.path.exists(identifier):
        config_file = identifier
        config = config_parser.load_config_file(config_file)
        step_class, name = Step._parse_class_and_name(config, config_file=config_file)
    else:
        try:
            step_class = utilities.import_class(
                utilities.resolve_step_class_alias(identifier), Step
            )
        except (ImportError, AttributeError, TypeError) as err:
            raise ValueError(
                f"{identifier!r} is not a path to a config file or a Python Step class"
            ) from err
        # Don't validate yet
        config = config_parser.config_from_dict({})
        name = None
        config_file = None

    return step_class, config, name, config_file


def _build_arg_parser_from_spec(spec, step_class, parent=None):
    """
    Given a configspec, sets up an argparse argument parser that
    understands its arguments.

    The \"path\" in the configspec becomes a dot-separated identifier
    in the commandline arguments.  For example, in the following
    configfile::

        [foo]
          [[bar]]
             baz = 2

    The "baz" variable can be changed with ``--foo.bar.baz=42``.
    """
    import argparse

    # It doesn't translate the configspec types -- it instead
    # will accept any string.  However, the types of the arguments will
    # later be verified by configobj itself.
    parser = argparse.ArgumentParser(
        parents=[parent],
        description=step_class.__doc__,
    )

    def build_from_spec(subspec, parts=None):
        if parts is None:
            parts = []
        for key, val in subspec.items():
            if isinstance(val, dict):
                build_from_spec(val, [*parts, key])
            else:
                comment = subspec.inline_comments.get(key) or ""
                comment = comment.lstrip("#").strip()
                # Only show default value if it is not None or the empty string
                default_value_string = val.split("(")[1].rstrip(")").strip()
                if default_value_string.lstrip("default=") in ["None", "''", '""']:
                    help_string = comment
                else:
                    help_string = f"{comment} [{default_value_string}]"
                argument = "--" + ".".join([*parts, key])
                if argument[2:] in built_in_configuration_parameters:
                    raise ValueError(
                        "The Step's spec is trying to override a built-in parameter"
                        f" {argument!r}"
                    )
                parser.add_argument(
                    "--" + ".".join([*parts, key]),
                    type=str,
                    help=help_string,
                    metavar="",
                )

    build_from_spec(spec)

    parser.add_argument(
        "args",
        nargs="*",
        help="arguments to pass to step",
    )

    return parser


class FromCommandLine(str):
    """
    We need a way to distinguish between config values that come from
    a config file and those that come from the commandline.  For
    example, configfile paths must be resolved against the location of
    the config file.  Commandline paths must be resolved against the
    current working directory.  By setting all commandline overrides
    as instances of this class, we can later (in `config_parser.py`)
    use isinstance to see where the values came from.
    """


def _override_config_from_args(config, args):
    """
    Overrides any configuration values in `config` with values from the
    parsed commandline arguments `args`.
    """

    def set_value(subconf, key, val):
        root, sep, rest = key.partition(".")
        if rest:
            set_value(subconf.setdefault(root, {}), rest, val)
        else:
            val, comment = config._handle_value(val)
            if isinstance(val, str):
                subconf[root] = FromCommandLine(val)
            else:
                subconf[root] = val

    for key, val in vars(args).items():
        if val is not None:
            set_value(config, key, val)


def just_the_step_from_cmdline(args, cls=None, apply_log_cfg=False):
    """
    Create a step from a configuration file and return it.  Don't run it.

    Parameters
    ----------
    args : list of str
        Commandline arguments

    cls : Step class
        The step class to use.  If none is provided, the step

    apply_log_cfg : bool
        If True, apply the logging configuration. If False,
        any provided log configuration will be ignored.

    Returns
    -------
    step : Step instance
        If the config file has a `class` parameter, or the commandline
        specifies a class, the return value will be as instance of
        that class.

        Any parameters found in the config file or on the commandline
        will be set as member variables on the returned `Step`
        instance.

    step_class: Step class
        As defined by `cls` parameter or .cfg file.

    positional: list of strings
        Positional parameters after arg parsing

    DOES NOT RUN THE STEP
    """
    import argparse

    parser1 = argparse.ArgumentParser(
        description="Run an stpipe Step or Pipeline",
        add_help=False,
    )
    if cls is None:
        parser1.add_argument(
            "cfg_file_or_class",
            type=str,
            nargs=1,
            help="The configuration file or Python class to run",
        )
    else:
        parser1.add_argument(
            "--config-file",
            type=str,
            help="A configuration file to load parameters from",
        )
    parser1.add_argument(
        "--logcfg",
        type=str,
        help="The logging configuration file to load",
    )
    parser1.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Turn on all logging messages",
    )
    parser1.add_argument(
        "--debug",
        action="store_true",
        help="When an exception occurs, invoke the Python debugger, pdb",
    )
    parser1.add_argument(
        "--save-parameters",
        type=str,
        help="Save step parameters to specified file.",
    )
    parser1.add_argument(
        "--disable-crds-steppars",
        action="store_true",
        help="Disable retrieval of step parameter references files from CRDS",
    )
    known, _ = parser1.parse_known_args(args)

    try:
        if cls is None:
            step_class, config, name, config_file = _get_config_and_class(
                known.cfg_file_or_class[0]
            )
        else:
            config_file = known.config_file
            config = config_parser.load_config_file(config_file)
            step_class, name = Step._parse_class_and_name(
                config, config_file=config_file
            )
            step_class = cls

        if known.verbose:
            if known.logcfg is not None:
                raise ValueError(
                    "If --verbose is set, a logging configuration file may not be"
                    " provided"
                )
            log_config = io.BytesIO(log.MAX_CONFIGURATION)
        elif known.logcfg is not None:
            if not os.path.exists(known.logcfg):
                raise OSError(f"Logging config {known.logcfg!r} not found")
            log_config = known.logcfg
        else:
            log_config = log._find_logging_config_file()

        try:
            log_cfg = log.load_configuration(log_config)
        except Exception as e:
            raise ValueError(
                f"Error parsing logging config {log_config!r}:\n{e}"
            ) from e
        # globally apply the logging configuration since we're in cmdline mode
        if apply_log_cfg:
            log_cfg.apply()
    except Exception as e:
        _print_important_message("ERROR PARSING CONFIGURATION:", str(e))
        parser1.print_help()
        raise

    debug_on_exception = known.debug

    # Determine whether CRDS should be queried for step parameters
    disable_crds_steppars = get_disable_crds_steppars(known.disable_crds_steppars)

    # This creates a config object from the spec file of the step class merged with
    # the spec files of the superclasses of the step class and adds arguments for
    # all of the expected reference files

    # load_spec_file is a method of both Step and Pipeline
    spec = step_class.load_spec_file()

    parser2 = _build_arg_parser_from_spec(spec, step_class, parent=parser1)

    args = parser2.parse_args(args)

    if cls is None:
        del args.cfg_file_or_class
    else:
        del args.config_file
    del args.logcfg
    del args.verbose
    del args.debug
    del args.save_parameters
    del args.disable_crds_steppars
    positional = args.args
    del args.args

    # This updates config (a ConfigObj) with the values from the command line arguments
    # Config is empty if class specified, otherwise contains values from config file
    # specified on command line
    _override_config_from_args(config, args)

    config = step_class.merge_config(config, config_file)

    if len(positional):
        input_file = positional[0]
        if args.input_dir:
            input_file = args.input_dir + "/" + input_file

        # Attempt to retrieve Step parameters from CRDS
        try:
            parameter_cfg = step_class.get_config_from_reference(
                input_file, disable=disable_crds_steppars
            )
        except (FileNotFoundError, OSError):
            logger.warning("Unable to open input file, cannot get parameters from CRDS")
        else:
            if config:
                config_parser.merge_config(parameter_cfg, config)
            config = parameter_cfg
    else:
        logger.info("No input file specified, unable to retrieve parameters from CRDS")

    # This is where the step is instantiated
    try:
        step = step_class.from_config_section(
            config,
            name=name,
            config_file=config_file,
        )
    except config_parser.ValidationError as e:
        # If the configobj validator failed, print usage information.
        _print_important_message("ERROR PARSING CONFIGURATION:", str(e))
        parser2.print_help()
        raise ValueError(str(e)) from e

    # Define the primary input file.
    # Always have an output_file set on the outermost step
    if len(positional):
        step.set_primary_input(positional[0])
        step.save_results = True

    # Save the step configuration
    if known.save_parameters:
        step.export_config(known.save_parameters, include_metadata=True)
        logger.info(f"Step/Pipeline parameters saved to '{known.save_parameters}'")

    return step, step_class, positional, debug_on_exception


def step_from_cmdline(args, cls=None):
    """
    Create a step from a configuration file and run it.

    Parameters
    ----------
    args : list of str
        Commandline arguments

    cls : Step class
        The step class to use.  If none is provided, the step

    Returns
    -------
    step : Step instance
        If the config file has a `class` parameter, or the commandline
        specifies a class, the return value will be as instance of
        that class.

        Any parameters found in the config file or on the commandline
        will be set as member variables on the returned `Step`
        instance.
    """
    try:
        step, step_class, positional, debug_on_exception = just_the_step_from_cmdline(
            args,
            cls,
            apply_log_cfg=True,
        )
    except Exception as e:
        # since we applied a log config above, undo it
        if log.LogConfig.applied is not None:
            log.LogConfig.applied.undo()
        raise e

    try:
        profile_path = os.environ.pop("STPIPE_PROFILE", None)
        if profile_path:
            import cProfile

            cProfile.runctx("step.run(*positional)", globals(), locals(), profile_path)
        else:
            step.run(*positional)
    except Exception as e:
        _print_important_message(f"ERROR RUNNING STEP {step_class.__name__!r}:", str(e))

        if debug_on_exception:
            import pdb  # noqa: T100

            pdb.post_mortem()
        else:
            raise
    finally:
        # since we applied a log config above, undo it
        if log.LogConfig.applied is not None:
            log.LogConfig.applied.undo()

    return step


def step_script(cls):
    import sys

    if not issubclass(cls, Step):
        raise AssertionError("cls must be a subclass of Step")

    return step_from_cmdline(sys.argv[1:], cls=cls)

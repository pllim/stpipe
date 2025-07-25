"""
Step
"""

import gc
import logging
import os
import sys
from collections.abc import Sequence
from contextlib import contextmanager, nullcontext, suppress
from functools import partial
from os.path import (
    abspath,
    basename,
    dirname,
    expanduser,
    expandvars,
    isfile,
    join,
    split,
    splitext,
)
from pathlib import Path
from typing import ClassVar

import yaml

try:
    from astropy.io import fits

    DISCOURAGED_TYPES = (fits.HDUList,)
except ImportError:
    DISCOURAGED_TYPES = None

from . import config, config_parser, crds_client, log, utilities
from .datamodel import AbstractDataModel
from .format_template import FormatTemplate
from .library import AbstractModelLibrary
from .utilities import _not_set

logger = logging.getLogger(log.STPIPE_ROOT_LOGGER)


class Step:
    """
    Step
    """

    spec = """
    pre_hooks          = list(default=list())        # List of Step classes to run before step
    post_hooks         = list(default=list())        # List of Step classes to run after step
    output_file        = output_file(default=None)   # File to save output to.
    output_dir         = string(default=None)        # Directory path for output files
    output_ext         = string()                    # Default type of output
    output_use_model   = boolean(default=False)      # When saving use `DataModel.meta.filename`
    output_use_index   = boolean(default=True)       # Append index.
    save_results       = boolean(default=False)      # Force save results
    skip               = boolean(default=False)      # Skip this step
    suffix             = string(default=None)        # Default suffix for output files
    search_output_file = boolean(default=True)       # Use outputfile define in parent step
    input_dir          = string(default=None)        # Input directory
    """  # noqa: E501
    # Nickname used to refer to this class in lieu of the fully-qualified class
    # name.  Must be globally unique!
    class_alias = None

    # Correction parameters. These store and use whatever information a Step
    # may need to perform its operations without re-calculating, or to use
    # from a previous run of the Step.  The structure is up to each Step.
    correction_pars = None
    use_correction_pars = False

    # Reference types for both command line override
    # definition and reference prefetch
    reference_file_types: ClassVar = []

    # Set to False in subclasses to skip prefetch,
    # but by default attempt to prefetch
    prefetch_references = True

    # This needs to be set to a logging formatter for any
    # log_records to be saved.
    _log_records_formatter = None

    @classmethod
    def get_config_reftype(cls):
        """
        Get the CRDS reftype for this step's config reference.

        Returns
        -------
        str
        """
        return f"pars-{cls.__name__.lower()}"

    @classmethod
    def merge_config(cls, config, config_file):
        return config

    @classmethod
    def load_spec_file(cls, preserve_comments=_not_set):
        spec = config_parser.get_merged_spec_file(
            cls, preserve_comments=preserve_comments
        )
        # Add arguments for all of the expected reference files
        for reference_file_type in cls.reference_file_types:
            override_name = crds_client.get_override_name(reference_file_type)
            spec[override_name] = "is_string_or_datamodel(default=None)"
            spec.inline_comments[override_name] = (
                f"# Override the {reference_file_type} reference file"
            )
        return spec

    @classmethod
    def print_configspec(cls):
        specfile = cls.load_spec_file()
        specfile.write(sys.stdout.buffer)

    @classmethod
    def from_config_file(cls, config_file, parent=None, name=None):
        """
        Create a step from a configuration file.

        Parameters
        ----------
        config_file : path or readable file-like object
            The config file to load parameters from

        parent : Step instance, optional
            The parent step of this step.  Used to determine a
            fully-qualified name for this step, and to determine
            the mode in which to run this step.

        name : str, optional
            If provided, use that name for the returned instance.
            If not provided, the following are tried (in order):
            - The ``name`` parameter in the config file
            - The filename of the config file
            - The name of returned class

        Returns
        -------
        step : Step instance
            If the config file has a ``class`` parameter, the return
            value will be as instance of that class.  The ``class``
            parameter in the config file must specify a subclass of
            ``cls``.  If the configuration file has no ``class``
            parameter, then an instance of ``cls`` is returned.

            Any parameters found in the config file will be set
            as member variables on the returned `Step` instance.
        """
        config = config_parser.load_config_file(config_file)

        # If a file object was passed in, pass the file name along
        if hasattr(config_file, "name"):
            config_file = config_file.name

        step_class, name = cls._parse_class_and_name(
            config,
            parent,
            name,
            config_file,
        )

        return step_class.from_config_section(
            config,
            parent=parent,
            name=name,
            config_file=config_file,
        )

    @staticmethod
    def from_cmdline(args):
        """
        Create a step from a configuration file.

        Parameters
        ----------
        args : list of str
            Commandline arguments

        Returns
        -------
        step : Step instance
            If the config file has a ``class`` parameter, the return
            value will be as instance of that class.

            Any parameters found in the config file will be set
            as member variables on the returned `Step` instance.
        """
        from . import cmdline

        return cmdline.step_from_cmdline(args)

    @classmethod
    def _parse_class_and_name(
        cls,
        config,
        parent=None,
        name=None,
        config_file=None,
    ):
        if "class" in config:
            step_class = utilities.import_class(
                utilities.resolve_step_class_alias(config["class"]),
                config_file=config_file,
            )
            if not issubclass(step_class, cls):
                raise TypeError(
                    "Configuration file does not match the expected step class. "
                    f" Expected {cls}, got {step_class}"
                )
        else:
            step_class = cls

        if not name:
            name = config.get("name")
            if not name:
                if isinstance(config_file, str):
                    name = splitext(basename(config_file))[0]
                else:
                    name = step_class.__name__

        if "name" in config:
            del config["name"]
        if "class" in config:
            del config["class"]

        return step_class, name

    @classmethod
    def from_config_section(
        cls,
        config,
        parent=None,
        name=None,
        config_file=None,
    ):
        """
        Create a step from a configuration file fragment.

        Parameters
        ----------
        config : configobj.Section instance
            The config file fragment containing parameters for this
            step only.
        parent : Step instance, optional
            The parent step of this step.  Used to determine a
            fully-qualified name for this step, and to determine
            the mode in which to run this step.
        name : str, optional
            If provided, use that name for the returned instance.
            If not provided, try the following (in order):
            - The ``name`` parameter in the config file fragment
            - The name of returned class
        config_file : str or pathlib.Path, optional
            The path to the config file that created this step, if
            any.  This is used to resolve relative file name
            parameters in the config file.

        Returns
        -------
        step : instance of cls
            Any parameters found in the config file fragment will be
            set as member variables on the returned `Step` instance.
        """
        if not name:
            if config.get("name"):
                name = config["name"]
            else:
                name = cls.__name__

        if "name" in config:
            del config["name"]
        if "class" in config:
            del config["class"]
        if "config_file" in config:
            del config["config_file"]

        spec = cls.load_spec_file()
        config = cls.merge_config(config, config_file)
        config_parser.validate(config, spec, root_dir=dirname(config_file or ""))

        if "config_file" in config:
            del config["config_file"]
        if "name" in config:
            del config["name"]

        # cmdline.FromCommandLine instances should not be passed to
        # steps. Instead, convert them back to strings.
        from . import cmdline

        kwargs = {}
        for k in config:
            if isinstance(config[k], cmdline.FromCommandLine):
                kwargs[k] = str(config[k])
            else:
                kwargs[k] = config[k]

        return cls(
            name=name,
            parent=parent,
            config_file=config_file,
            _validate_kwds=False,
            **kwargs,
        )

    @classmethod
    def _get_filename(cls, dataset):
        """
        Class method to get a filename for a dataset.

        Parameters
        ----------
        dataset : str, Path, DataModel, ModelLibrary, Sequence
            Dataset to be inspected for a filename.

        Returns
        -------
        filename : str or None
            Filename as a string or None if no filename could be determined.
        """
        if isinstance(dataset, str):
            dataset = Path(dataset)

        if isinstance(dataset, Path):
            return dataset.name

        if isinstance(dataset, Sequence):
            if not len(dataset):
                return None
            dataset = dataset[0]

        if isinstance(dataset, AbstractDataModel):
            return cls._get_filename(dataset.meta.filename)

        if isinstance(dataset, AbstractModelLibrary):
            return cls._get_filename(dataset.asn.get("table_name", None))

        return None

    @classmethod
    def _get_crds_parameters(cls, dataset):
        """
        Class method to get the CRDS parameters and observatory for a given dataset.

        Parameters
        ----------
        dataset : str, Path, DataModel, ModelLibrary, Sequence
            Dataset to use for determining CRDS parameters.

        Returns
        -------
        parameters : dict
            Dictionary of parameters to pass to CRDS.
        observatory : str
            Observatory to pass to CRDS.
        """
        if isinstance(dataset, AbstractModelLibrary) or (
            isinstance(dataset, AbstractDataModel) and not isinstance(dataset, Sequence)
        ):
            return (
                dataset.get_crds_parameters(),
                dataset.crds_observatory,
            )

        if isinstance(dataset, str):
            dataset = Path(dataset)

        # for associations, only open the first science member
        if isinstance(dataset, Path) and dataset.suffix.lower() == ".json":
            open_kwargs = {"asn_n_members": 1, "asn_exptypes": ["science"]}
        else:
            open_kwargs = {}

        with cls._datamodels_open(dataset, **open_kwargs) as model:
            # ModelContainer is a Sequence, use the first model
            if isinstance(model, Sequence):
                model = model[0]

            return cls._get_crds_parameters(model)

    def __init__(
        self,
        name=None,
        parent=None,
        config_file=None,
        _validate_kwds=True,
        **kws,
    ):
        """
        Create a `Step` instance.

        Parameters
        ----------
        name : str, optional
            The name of the Step instance.  Used in logging messages
            and in cache filenames.  If not provided, one will be
            generated based on the class name.

        parent : Step instance, optional
            The parent step of this step.  Used to determine a
            fully-qualified name for this step, and to determine
            the mode in which to run this step.

        config_file : str or pathlib.Path, optional
            The path to the config file that this step was initialized
            with.  Use to determine relative path names of other config files.

        **kws : dict
            Additional parameters to set.  These will be set as member
            variables on the new Step instance.
        """
        self._reference_files_used = []
        # A list of logging.LogRecord emitted to the stpipe root logger
        # during the most recent call to Step.run.
        self._log_records = []
        self._input_filename = None
        self._input_dir = None
        self._keywords = kws
        if _validate_kwds:
            spec = self.load_spec_file()
            kws = config_parser.config_from_dict(
                kws,
                spec,
                root_dir=dirname(config_file or ""),
            )

        if name is None:
            name = self.__class__.__name__
        self.name = name
        if parent is None:
            self.qualified_name = f"{log.STPIPE_ROOT_LOGGER}.{self.name}"
        else:
            self.qualified_name = f"{parent.qualified_name}.{self.name}"
        self.parent = parent

        # Set the parameters as member variables
        for key, val in kws.items():
            setattr(self, key, val)

        # Create a new logger for this step
        self.log = logging.getLogger(self.qualified_name)

        self.log.setLevel(log.logging.DEBUG)

        # Log the fact that we have been init-ed.
        self.log.info(
            "%s instance created.",
            self.__class__.__name__,
        )

        # Store the config file path so config filenames can be resolved
        # against it.
        self.config_file = config_file

        # Setup the hooks
        if len(self.pre_hooks) or len(self.post_hooks):
            from . import hooks

            self._pre_hooks = hooks.get_hook_objects(self, "pre", self.pre_hooks)
            self._post_hooks = hooks.get_hook_objects(self, "post", self.post_hooks)
        else:
            self._pre_hooks = []
            self._post_hooks = []

    def _check_args(self, args, discouraged_types, msg):
        if discouraged_types is None:
            return

        if type(args) not in (list, tuple):
            args = [args]

        for i, arg in enumerate(args):
            if isinstance(arg, discouraged_types):
                self.log.error(
                    "%s %s object.  Use an instance of AbstractDataModel instead.",
                    msg,
                    i,
                )

    @property
    def log_records(self):
        """
        Retrieve logs from the most recent run of this step.

        Returns
        -------
        list of str
        """
        return self._log_records

    def run(self, *args):
        """
        Run handles the generic setup and teardown that happens with
        the running of each step.  The real work that is unique to
        each step type is done in the `process` method.
        """
        gc.collect()

        with log.record_logs(formatter=self._log_records_formatter) as log_records:
            self._log_records = log_records

            step_result = None

            self.log.info("Step %s running with args %s.", self.name, args)
            # log Step or Pipeline parameters from top level only
            if self.parent is None:
                self.log.info(
                    "Step %s parameters are:%s",
                    self.name,
                    # Add an indent to each line of the YAML output
                    "\n  "
                    + "\n  ".join(
                        yaml.dump(self.get_pars(), sort_keys=False)
                        .strip()
                        # Convert serialized YAML types true/false/null to Python types
                        .replace(" false", " False")
                        .replace(" true", " True")
                        .replace(" null", " None")
                        .splitlines()
                    ),
                )

            if len(args):
                self.set_primary_input(args[0])

            # Default output file configuration
            if self.output_file is not None:
                self.save_results = True

            if self.suffix is None:
                self.suffix = self.default_suffix()

            hook_args = args
            for pre_hook in self._pre_hooks:
                hook_results = pre_hook.run(*hook_args)
                if hook_results is not None:
                    hook_args = (hook_results,)
            args = hook_args

            self._reference_files_used = []

            # Warn if passing in objects that should be
            # discouraged.
            self._check_args(args, DISCOURAGED_TYPES, "Passed")
            if self.parent is None:
                if self.skip:
                    self.log.info("Step run as standalone, so skip set to False")
                    self.skip = False
            # Run the Step-specific code.
            if self.skip:
                self.log.info("Step skipped.")

                if self.class_alias is not None:

                    def set_skipped(model):
                        try:
                            setattr(model.meta.cal_step, self.class_alias, "SKIPPED")
                        except AttributeError as e:
                            self.log.info(
                                "Could not record skip into DataModel " "header: %s",
                                e,
                            )

                    if isinstance(args[0], AbstractModelLibrary):
                        list(args[0].map_function(lambda m, i: set_skipped(m)))
                    elif isinstance(args[0], AbstractDataModel):
                        if isinstance(args[0], Sequence):
                            [set_skipped(m) for m in args[0]]
                        else:
                            set_skipped(args[0])
                step_result = args[0]
            else:
                if self.prefetch_references:
                    self.prefetch(*args)
                try:
                    step_result = self.process(*args)
                except TypeError as e:
                    if "process() takes exactly" in str(e):
                        raise TypeError("Incorrect number of arguments to step") from e
                    raise

            # Warn if returning a discouraged object
            self._check_args(step_result, DISCOURAGED_TYPES, "Returned")

            # Run the post hooks
            for post_hook in self._post_hooks:
                hook_results = post_hook.run(step_result)
                if hook_results is not None:
                    step_result = hook_results

            # Update meta information
            if isinstance(step_result, AbstractModelLibrary):
                step_result.finalize_result(self, self._reference_files_used)
            else:
                if not isinstance(step_result, Sequence):
                    results = [step_result]
                else:
                    results = step_result

                # The finalize_result hook allows subclasses to add
                # metadata (like the cal code package version) before
                # the result is saved.
                for result in results:
                    self.finalize_result(result, self._reference_files_used)

            self._reference_files_used = []

            # Save the output file if one was specified
            if not self.skip and self.save_results:
                # Setup the save list.
                if not isinstance(step_result, list | tuple):
                    results_to_save = [step_result]
                else:
                    results_to_save = step_result

                for idx, result in enumerate(results_to_save):
                    if len(results_to_save) <= 1:
                        idx = None
                    if isinstance(result, (AbstractDataModel | AbstractModelLibrary)):
                        self.save_model(result, idx=idx)
                    elif hasattr(result, "save"):
                        try:
                            output_path = self.make_output_path(idx=idx)
                        except AttributeError:
                            self.log.warning(
                                "`save_results` has been requested, but cannot"
                                " determine filename."
                            )
                            self.log.warning(
                                "Specify an output file with `--output_file` or set"
                                " `--save_results=false`"
                            )
                        else:
                            self.log.info("Saving file %s", output_path)
                            result.save(output_path, overwrite=True)

            if not self.skip:
                self.log.info("Step %s done", self.name)

        return step_result

    def finalize_result(self, result, reference_files_used):
        """
        Hook that allows subclasses to set mission-specific metadata on each
        step result before that result is saved.

        Parameters
        ----------
        result : a datamodel that is an instance of AbstractDataModel or
                 collections.abc.Sequence
                 One step result (potentially of many).

        reference_files_used : list of tuple
            List of reference files used when running the step, each
            a tuple in the form (str reference type, str reference URI).
        """

    @staticmethod
    def remove_suffix(name):
        """
        Remove a known Step filename suffix from a filename
        (if present).

        Parameters
        ----------
        name : str
            Filename.

        Returns
        -------
        str
            Filename with any known suffix removed.
        str
            Separator that delimited the original suffix.
        """
        return name, "_"

    def prefetch(self, *args):
        """Prefetch reference files,  nominally called when
        self.prefetch_references is True.  Can be called explicitly
        when self.prefetch_refences is False.
        """
        # prefetch truly occurs at the Pipeline (or subclass) level.
        if len(args) and len(self.reference_file_types) and not self.skip:
            self._precache_references(args[0])

    def process(self, *args):
        """
        This is where real work happens. Every Step subclass has to
        override this method. The default behaviour is to raise a
        NotImplementedError exception.
        """
        raise NotImplementedError("Steps have to override process().")

    @classmethod
    def call(cls, *args, **kwargs):
        """
        Creates and runs a new instance of the class.

        Gets a config file from CRDS if one is available

        To set configuration parameters, pass a ``config_file`` path or
        keyword arguments.  Keyword arguments override those in the
        specified ``config_file``.

        Any positional ``*args`` will be passed along to the step's
        ``process`` method.

        Note: this method creates a new instance of `Step` with the given
        ``config_file`` if supplied, plus any extra ``*args`` and ``**kwargs``.
        If you create an instance of a Step, set parameters, and then use
        this ``call()`` method, it will ignore previously-set parameters, as
        it creates a new instance of the class with only the ``config_file``,
        ``*args`` and ``**kwargs`` passed to the ``call()`` method.

        If not used with a ``config_file`` or specific ``*args`` and ``**kwargs``,
        it would be better to use the `run` method, which does not create
        a new instance but simply runs the existing instance of the `Step`
        class.
        """
        filename = None
        if len(args) > 0:
            filename = args[0]

        # set up the log configuration here (although we might undo it
        # below) as log messages are generated before the config is
        # fully loaded
        if "logcfg" in kwargs:
            try:
                log_cfg = log.load_configuration(kwargs["logcfg"])
            except Exception as e:
                raise RuntimeError(
                    f"Error parsing logging config {kwargs['logcfg']}"
                ) from e
            del kwargs["logcfg"]
        elif log.LogConfig.applied is None:
            log_cfg = log.load_configuration(log._find_logging_config_file())
        else:
            log_cfg = None
        ctx = nullcontext if log_cfg is None else log_cfg.context

        with ctx():
            config, config_file = cls.build_config(filename, **kwargs)

            if "logcfg" in config:
                # a logcfg is in the configuration file
                if log_cfg is not None:
                    log_cfg.undo()
                log_cfg = log.load_configuration(config["logcfg"])
                log_cfg.apply()

            if "class" in config:
                del config["class"]

            name = config.get("name", None)
            instance = cls.from_config_section(
                config, name=name, config_file=config_file
            )

            return instance.run(*args)

    @property
    def input_dir(self):
        return self.search_attr("_input_dir", "")

    @input_dir.setter
    def input_dir(self, input_dir):
        self._input_dir = input_dir

    def default_output_file(self, input_file=None):
        """Create a default filename based on the input name"""
        output_file = input_file
        if output_file is None or not isinstance(output_file, str):
            output_file = self.search_attr("_input_filename")
        if output_file is None:
            output_file = f"step_{self.name}{self.output_ext}"
        return output_file

    def default_suffix(self):
        """Return a default suffix based on the step"""
        return self.name.lower()

    def search_attr(self, attribute, default=None, parent_first=False):
        """Return first non-None attribute in step hierarchy

        Parameters
        ----------
        attribute : str
            The attribute to retrieve

        default : obj
            If attribute is not found, the value to use

        parent_first : bool
            If `True`, allow parent definition to override step version

        Returns
        -------
        value : obj
            Attribute value or default if not found
        """
        if parent_first:
            try:
                value = self.parent.search_attr(attribute, parent_first=parent_first)
            except AttributeError:
                value = None
            if value is None:
                value = getattr(self, attribute, default)
            return value

        value = getattr(self, attribute, None)
        if value is None:
            try:
                value = self.parent.search_attr(attribute)
            except AttributeError:
                pass
        if value is None:
            value = default
        return value

    def _precache_references(self, input_file):
        """Because Step precaching precedes calls to get_reference_file() almost
        immediately, true precaching has been moved to Pipeline where the
        interleaving of precaching and Step processing is more of an
        issue. This null method is intended to be overridden in Pipeline by
        true precache operations and avoids having to override the more complex
        Step.run() instead.
        """

    def get_ref_override(self, reference_file_type):
        """Determine and return any override for ``reference_file_type``.

        Returns
        -------
        override_filepath or None.
        """
        override_name = crds_client.get_override_name(reference_file_type)
        path = getattr(self, override_name, None)
        if isinstance(path, AbstractDataModel):
            return path

        return abspath(path) if path and path != "N/A" else path

    def get_reference_file(self, input_file, reference_file_type):
        """
        Get a reference file from CRDS.

        If the configuration file or commandline parameters override the
        reference file, it will be automatically used when calling this
        function.

        Parameters
        ----------
        input_file : a datamodel that is an instance of AbstractDataModel
            A model of the input file.  Metadata on this input file
            will be used by the CRDS "bestref" algorithm to obtain a
            reference file.

        reference_file_type : string
            The type of reference file to retrieve.  For example, to
            retrieve a flat field reference file, this would be 'flat'.

        Returns
        -------
        reference_file : path of reference file,  a string
        """
        override = self.get_ref_override(reference_file_type)
        if override is not None:
            if isinstance(override, AbstractDataModel):
                self._reference_files_used.append(
                    (reference_file_type, override.override_handle)
                )
                return override

            if override.strip() != "":
                self._reference_files_used.append(
                    (reference_file_type, abspath(override))
                )
                reference_name = override
            else:
                return ""
        else:
            parameters, observatory = self._get_crds_parameters(input_file)
            reference_name = crds_client.get_reference_file(
                parameters,
                reference_file_type,
                observatory,
            )
            if reference_name != "N/A":
                hdr_name = "crds://" + basename(reference_name)
            else:
                hdr_name = "N/A"
            self._reference_files_used.append((reference_file_type, hdr_name))
        return crds_client.check_reference_open(reference_name)

    @classmethod
    def get_config_from_reference(cls, dataset, disable=None, crds_observatory=None):
        """Retrieve step parameters from reference database

        Parameters
        ----------
        cls : stpipe.Step
            Either a class or instance of a class derived
            from `Step`.
        dataset : AbstractDataModel or dict
            A model of the input file.  Metadata on this input file will
            be used by the CRDS "bestref" algorithm to obtain a reference
            file. If a dict, crds_observatory must be a non-None value.
        disable: bool or None
            Do not retrieve parameters from CRDS. If None, check global settings.
        crds_observatory : str
            Observatory name ('jwst' or 'roman').

        Returns
        -------
        step_parameters : configobj
            The parameters as retrieved from CRDS. If there is an issue, log as such
            and return an empty config obj.
        """

        reftype = cls.get_config_reftype()

        if isinstance(dataset, dict):
            # crds_parameters was passed as input from pipeline.py
            crds_parameters = dataset
            if crds_observatory is None:
                raise ValueError("Need a valid name for crds_observatory.")
        else:
            # If the dataset is not an operable instance of AbstractDataModel,
            # log as such and return an empty config object
            try:
                crds_parameters, crds_observatory = cls._get_crds_parameters(dataset)
            except (OSError, TypeError, ValueError):
                logger.warning("Input dataset is not an instance of AbstractDataModel.")
                disable = True

        # Check if retrieval should be attempted.
        if disable is None:
            disable = get_disable_crds_steppars()
        if disable:
            logger.info(
                "%s: CRDS parameter reference retrieval disabled.", reftype.upper()
            )
            return config_parser.ConfigObj()

        # Retrieve step parameters from CRDS
        logger.debug("Retrieving step %s parameters from CRDS", reftype.upper())
        try:
            ref_file = crds_client.get_reference_file(
                crds_parameters,
                reftype,
                crds_observatory,
            )
        except (AttributeError, crds_client.CrdsError):
            logger.debug("%s: No parameters found", reftype.upper())
            return config_parser.ConfigObj()
        if ref_file != "N/A":
            logger.info("%s parameters found: %s", reftype.upper(), ref_file)
            ref = config_parser.load_config_file(ref_file)

            ref_pars = {
                par: value for par, value in ref.items() if par not in ["class", "name"]
            }
            logger.debug(
                "%s parameters retrieved from CRDS: %s", reftype.upper(), ref_pars
            )

            return ref

        logger.debug("No %s reference files found.", reftype.upper())
        return config_parser.ConfigObj()

    def set_primary_input(self, obj, exclusive=True):
        """
        Sets the name of the master input file and input directory.
        Used to generate output file names.

        Parameters
        ----------
        obj : str, pathlib.Path, or instance of AbstractDataModel
            The object to base the name on. If a datamodel,
            use Datamodel.meta.filename.

        exclusive : bool
            If True, only set if an input name is not already used
            by a parent Step. Otherwise, always set.
        """
        self._set_input_dir(obj, exclusive=exclusive)

        err_message = f"Cannot set master input file name from object {obj}"
        parent_input_filename = self.search_attr("_input_filename")
        if not exclusive or parent_input_filename is None:
            if isinstance(obj, str | Path):
                self._input_filename = str(obj)
            elif isinstance(obj, AbstractDataModel):
                try:
                    self._input_filename = obj.meta.filename
                except AttributeError:
                    self.log.debug(err_message)
            else:
                self.log.debug(err_message)

    def save_model(
        self,
        model,
        suffix=None,
        idx=None,
        output_file=None,
        force=False,
        **components,
    ):
        """
        Saves the given model using the step/pipeline's naming scheme

        Parameters
        ----------
        model : a instance of AbstractDataModel
            The model to save.

        suffix : str
            The suffix to add to the filename.

        idx : object
            Index identifier.

        output_file : str
            Use this file name instead of what the Step
            default would be.

        force : bool
            Regardless of whether ``save_results`` is `False`
            and no ``output_file`` is specified, try saving.

        components : dict
            Other components to add to the file name.

        Returns
        -------
        output_paths : [str[, ...]]
            List of output file paths the model(s) were saved in.
        """
        if output_file is None or output_file == "":
            output_file = self.output_file

        # Check if saving is even specified.
        if not force and not self.save_results and not output_file:
            return None

        if isinstance(model, AbstractModelLibrary):
            output_paths = []
            with model:
                for i, m in enumerate(model):
                    output_paths.append(
                        self.save_model(
                            m,
                            idx=i,
                            suffix=suffix,
                            force=force,
                            **components,
                        )
                    )
                    # leaving modify=True in case saving modify the file
                    model.shelve(m, i)
            return output_paths
        elif isinstance(model, Sequence):
            save_model_func = partial(
                self.save_model,
                suffix=suffix,
                force=force,
                **components,
            )
            output_path = model.save(
                path=output_file,
                save_model_func=save_model_func,
            )
        else:
            # Search for an output file name.
            if self.output_use_model or (
                output_file is None and not self.search_output_file
            ):
                output_file = model.meta.filename
                idx = None
            output_path = model.save(
                self.make_output_path(
                    basepath=output_file,
                    suffix=suffix,
                    idx=idx,
                    **components,
                )
            )
            self.log.info("Saved model in %s", output_path)

        return output_path

    @property
    def make_output_path(self):
        """Return function that creates the output path"""
        make_output_path = self.search_attr("_make_output_path")
        return partial(make_output_path, self)

    @staticmethod
    def _make_output_path(
        step,
        basepath=None,
        ext=None,
        suffix=None,
        **components,
    ):
        """Create the output path

        Parameters
        ----------
        step : Step
            The `Step` in question.

        basepath : str or None
            The basepath to use. If None, `output_file`
            is used. Only the basename component of the path
            is used.

        ext : str or None
            The extension to use. If none, `output_ext` is used.
            Can include the leading period or not.

        suffix : str or None or False
            Suffix to append to the filename.
            If None, the `Step` default will be used.
            If False, no suffix replacement will be done.

        components : dict
            dict of string replacements.

        Returns
        -------
        The fully qualified path name.

        Notes
        -----
        The values found in the `components` dict are placed in the string
        where the "{components}" replacement field is specified separated by
        underscores.
        """
        separator = "_"
        if basepath is None and step.search_output_file:
            basepath = step.search_attr("output_file")
        if basepath is None:
            basepath = step.default_output_file()

        basename, basepath_ext = splitext(split(basepath)[1])
        if ext is None:
            ext = step.output_ext
        if ext is None and len(basepath_ext):
            ext = basepath_ext
        if ext.startswith("."):
            ext = ext[1:]

        # Suffix check. An explicit check on `False` is necessary
        # because `None` is also allowed.
        suffix = _get_suffix(suffix, step=step)
        if suffix is not False:
            default_name_format = "{basename}{components}{suffix_sep}{suffix}.{ext}"
            suffix_sep = None
            if suffix is not None:
                basename, suffix_sep = step.remove_suffix(basename)
            if suffix_sep is None:
                suffix_sep = separator
        else:
            default_name_format = "{basename}{components}.{ext}"
            suffix = None
            suffix_sep = None

        # Setup formatting
        formatter = FormatTemplate(
            separator=separator,
            remove_unused=True,
        )

        if len(components):
            component_str = formatter("", **components)
        else:
            component_str = ""

        basename = formatter(
            default_name_format,
            basename=basename,
            suffix=suffix,
            suffix_sep=suffix_sep,
            ext=ext,
            components=component_str,
        )

        output_dir = step.search_attr("output_dir", default="")
        output_dir = expandvars(expanduser(output_dir))
        return join(output_dir, basename)

    @classmethod
    def _datamodels_open(cls, init, **kwargs):
        """
        Wrapper around observatory-specific datamodels.open function.
        """
        raise NotImplementedError(f"{cls.__name__} does not implement _datamodels_open")

    def open_model(self, init, **kwargs):
        """Open a datamodel

        Primarily a wrapper around ``DataModel.open`` to
        handle `Step` peculiarities

        Parameters
        ----------
        init : object
            The object to open

        Returns
        -------
        datamodel : instance of AbstractDataModel
            Object opened as a datamodel
        """
        # Use the parent method if available, since this step
        # might be a hook that doesn't implement _datamodels_open.
        if self.parent is None:
            datamodels_open = self._datamodels_open
        else:
            datamodels_open = self.parent._datamodels_open

        return datamodels_open(self.make_input_path(init), **kwargs)

    def make_input_path(self, file_path):
        """Create an input path for a given file path

        If ``file_path`` has no directory path, use ``self.input_dir``
        as the directory path.

        Parameters
        ----------
        file_path : str or obj
            The supplied file path to check and modify.
            If anything other than `str`, the object
            is simply passed back.

        Returns
        -------
        full_path : str or obj
            File path using ``input_dir`` if the input
            had no directory path.
        """
        full_path = file_path
        if isinstance(file_path, str):
            original_path, file_name = split(file_path)
            if not len(original_path):
                full_path = join(self.input_dir, file_name)

        return full_path

    def _set_input_dir(self, input_, exclusive=True):
        """Set the input directory

        If sufficient information is at hand, set a value
        for the attribute `input_dir`.

        Parameters
        ----------
        input_ : str
            Input to determine path from.

        exclusive : bool
            If True, only set if an input directory is not already
            defined by a parent Step. Otherwise, always set.

        """
        if not exclusive or self.search_attr("_input_dir") is None:
            with suppress(Exception):
                if isfile(input_):
                    self.input_dir = split(input_)[0]

    def get_pars(self, full_spec=True):
        """Retrieve the configuration parameters of a step

        Parameters
        ----------
        full_spec : bool
            Return all parameters, including parent-specified parameters.
            If `False`, return only parameters specific to the step.

        Returns
        -------
        dict
            Keys are the parameters and values are the values.
        """
        from . import cmdline

        if full_spec:
            spec_file_func = config_parser.get_merged_spec_file
        else:
            spec_file_func = config_parser.load_spec_file
        spec = spec_file_func(self)
        if spec is None:
            return {}
        instance_pars = {}
        for key in spec:
            if hasattr(self, key):
                value = getattr(self, key)
                instance_pars[key] = value
        pars = config_parser.config_from_dict(instance_pars, spec, allow_missing=True)

        # Convert the config to a pure dict.
        pars_dict = {}
        for key, value in pars.items():
            if isinstance(value, cmdline.FromCommandLine):
                pars_dict[key] = str(value)
            else:
                pars_dict[key] = value
        return pars_dict

    def export_config(self, filename, include_metadata=False):
        """
        Export this step's parameters to an ASDF config file.

        Parameters
        ----------
        filename : str or pathlib.Path
            Path to config file.

        include_metadata : bool, optional
            Set to True to include metadata that is required
            for submission to CRDS.
        """
        with config.export_config(self).to_asdf(
            include_metadata=include_metadata
        ) as af:
            af.write_to(filename)

    def update_pars(self, parameters):
        """Update step parameters

        Only existing parameters are updated. Otherwise, new keys
        found in ``parameters`` are ignored.

        Parameters
        ----------
        parameters : dict
            Parameters to update.

        Notes
        -----
        ``parameters`` is presumed to have been produced by the
        `Step.get_pars` method. As such, the "steps" key is treated
        special in that it is a dict whose keys are the steps assigned
        directly as parameters to the current step. This is standard
        practice for `Pipeline`-based steps.
        """
        existing = self.get_pars().keys()
        for parameter, value in parameters.items():
            if parameter in existing:
                if parameter != "steps":
                    setattr(self, parameter, value)
                else:
                    for step_name, step_parameters in value.items():
                        getattr(self, step_name).update_pars(step_parameters)
            else:
                self.log.debug(
                    "Parameter %s is not valid for step %s. Ignoring.", parameter, self
                )

    @classmethod
    def build_config(cls, input, **kwargs):  # noqa: A002
        """Build the ConfigObj to initialize a Step

        A Step config is built in the following order:

        - CRDS parameter reference file
        - Local parameter reference file
        - Step keyword arguments

        Parameters
        ----------
        input : str or None
            Input file

        kwargs : dict
            Keyword arguments that specify Step parameters.

        Returns
        -------
        config, config_file : ConfigObj, str
            The configuration and the config filename.
        """
        logger_name = cls.__name__
        log_cls = logging.getLogger(logger_name)
        if input:
            config = cls.get_config_from_reference(input)
        else:
            log_cls.info("No filename given, cannot retrieve config from CRDS")
            config = config_parser.ConfigObj()

        if "config_file" in kwargs:
            config_file = kwargs["config_file"]
            del kwargs["config_file"]
            config_from_file = config_parser.load_config_file(str(config_file))
            config_parser.merge_config(config, config_from_file)
            config_dir = os.path.dirname(config_file)
        else:
            config_file = None
            config_dir = ""

        config_kwargs = config_parser.ConfigObj()

        # load and merge configuration files for each step they are provided:
        steps = {}
        if "steps" in kwargs:
            for step, pars in kwargs["steps"].items():
                if "config_file" in pars:
                    step_config_file = os.path.join(config_dir, pars["config_file"])
                    cfgd = config_parser.load_config_file(step_config_file)
                    if "name" in cfgd:
                        if cfgd["name"] != step:
                            raise ValueError(
                                "Step name from configuration file "
                                f"'{step_config_file}' does not match step "
                                "name in the 'steps' argument."
                            )
                        del cfgd["name"]
                    cfgd.pop("class", None)
                    cfgd.update(pars)
                    steps[step] = cfgd
                else:
                    steps[step] = pars

            kwargs = {k: v for k, v in kwargs.items() if k != "steps"}
            if steps:
                kwargs["steps"] = steps

        config_parser.merge_config(config_kwargs, kwargs)
        config_parser.merge_config(config, config_kwargs)

        return config, config_file


# #########
# Utilities
# #########


def _get_suffix(suffix, step=None, default_suffix=None):
    """Retrieve either specified or pipeline-supplied suffix

    Parameters
    ----------
    suffix : str or None
        Suffix to use if specified.

    step : Step or None
        The step to retrieve the suffix.

    default_suffix : str
        If the pipeline does not supply a suffix,
        use this.

    Returns
    -------
    suffix : str or None
        Suffix to use
    """
    if suffix is None and step is not None:
        suffix = step.search_attr("suffix")
    if suffix is None:
        suffix = default_suffix
    if suffix is None and step is not None:
        suffix = step.name.lower()
    return suffix


def get_disable_crds_steppars(default=None):
    """Return either the explicit default flag or retrieve from the environment

    If a default is not specified, retrieve the value from the environmental variable
    `STPIPE_DISABLE_CRDS_STEPPARS`.

    Parameters
    ----------
    default: str, bool, or None
        Flag to use. If None, the environmental is used.

    Returns
    -------
    flag: bool
        True to disable CRDS STEPPARS retrieval.
    """
    truths = ("true", "True", "t", "yes", "y")
    if default:
        if isinstance(default, bool):
            return default

        if isinstance(default, str):
            return default in truths

        raise ValueError(f"default must be string or boolean: {default}")

    flag = os.environ.get("STPIPE_DISABLE_CRDS_STEPPARS", "")
    return flag in truths


@contextmanager
def preserve_step_pars(step):
    """Context manager to preserve step parameters

    Ensure step parameters are not modified during a block
    of operations. Allows local reuse of a Step instance without
    having to worry about side-effects on that Step. If used with
    a `Pipeline`, all substep parameters are also restored.

    Yields
    ------
    saved_pars: dict
        The saved parameters.
    """
    saved_pars = step.get_pars()
    try:
        yield saved_pars
    finally:
        step.update_pars(saved_pars)

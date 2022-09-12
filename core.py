import asyncio
import logging
import click
from datetime import datetime
from multiprocessing import freeze_support
from cdisc_rules_engine.enums.report_types import ReportTypes
from cdisc_rules_engine.models.validation_args import Validation_args
from scripts.run_validation import run_validation
from cdisc_rules_engine.utilities.utils import generate_report_filename
from cdisc_rules_engine.services.cache.cache_populator_service import CachePopulator
from cdisc_rules_engine.config import config
from cdisc_rules_engine.services.cache.cache_service_factory import CacheServiceFactory
from cdisc_rules_engine.services.cdisc_library_service import CDISCLibraryService


@click.group()
def cli():
    pass


@click.command()
@click.option(
    "-ca",
    "--cache",
    default="cdisc_rules_engine/resources/cache",
    help="Relative path to cache files containing pre loaded metadata and rules",
)
@click.option(
    "-p",
    "--pool-size",
    default=10,
    type=int,
    help="Number of parallel processes for validation",
)
@click.option(
    "-d",
    "--data",
    required=True,
    help="Relative path to directory containing data files",
)
@click.option(
    "-l",
    "--log-level",
    default="disabled",
    type=click.Choice(["info", "debug", "error", "critical", "disabled"]),
    help="Sets log level for engine logs, logs are disabled by default",
)
@click.option(
    "-rt",
    "--report-template",
    default="cdisc_rules_engine/resources/templates/report-template.xlsx",
    help="File path of report template to use for excel output",
)
@click.option(
    "-s", "--standard", required=True, help="CDISC standard to validate against"
)
@click.option(
    "-v", "--version", required=True, help="Standard version to validate against"
)
@click.option(
    "-ct",
    "--controlled-terminology-package",
    multiple=True,
    help="Controlled terminology package to validate against,"
    " can provide more than one",
)
@click.option(
    "-o",
    "--output",
    default=generate_report_filename(datetime.now().isoformat()),
    help="Report output file destination",
)
@click.option(
    "-of",
    "--output-format",
    default=ReportTypes.XLSX.value,
    type=click.Choice(ReportTypes.values(), case_sensitive=False),
    help="Output file format",
)
@click.option(
    "-rr",
    "--raw-report",
    default=False,
    show_default=True,
    is_flag=True,
    help="Report in a raw format as it is generated by the engine. "
    "This flag must be used only with --output-format JSON.",
)
@click.option(
    "-dv",
    "--define-version",
    default="2.1",
    help="Define-XML version used for validation",
)
@click.option("--whodrug", help="Path to directory with WHODrug dictionary files")
@click.option("--meddra", help="Path to directory with MedDRA dictionary files")
@click.option(
    "--disable-progressbar",
    is_flag=True,
    default=False,
    show_default=True,
    help="Disable progress bar",
)
@click.pass_context
def validate(
    ctx,
    cache,
    pool_size,
    data,
    log_level,
    report_template,
    standard,
    version,
    controlled_terminology_package,
    output,
    output_format,
    raw_report,
    define_version,
    whodrug,
    meddra,
    disable_progressbar,
):
    """
    Validate data using CDISC Rules Engine

    Example:

    python core.py -s SDTM -v 3.4 -d /path/to/datasets
    """

    # Validate conditional options
    logger = logging.getLogger("validator")
    if raw_report is True and output_format.upper() != ReportTypes.JSON.value:
        logger.error("Flag --raw-report can be used only when --output-format is JSON")
        ctx.exit()

    run_validation(
        Validation_args(
            cache,
            pool_size,
            data,
            log_level,
            report_template,
            standard,
            version,
            controlled_terminology_package,
            output,
            output_format,
            raw_report,
            define_version,
            whodrug,
            meddra,
            disable_progressbar,
        )
    )


@click.command()
@click.option(
    "-c",
    "--cache_path",
    default="resources/cache",
    help="Relative path to cache files containing pre loaded metadata and rules",
)
@click.option(
    "--apikey",
    envvar="CDISC_LIBRARY_API_KEY",
    help="CDISC Library api key. Can be provided in the environment "
    "variable CDISC_LIBRARY_API_KEY",
    required=True,
)
@click.pass_context
def update_cache(ctx: click.Context, cache_path: str, apikey: str):
    cache = CacheServiceFactory(config).get_cache_service()
    library_service = CDISCLibraryService(apikey, cache)
    cache_populator = CachePopulator(cache, library_service)
    cache = asyncio.run(cache_populator.load_cache_data())
    cache_populator.save_rules_locally(cache_path)
    cache_populator.save_ct_packages_locally(cache_path)
    cache_populator.save_standards_metadata_locally(cache_path)
    cache_populator.save_variable_codelist_maps_locally(cache_path)
    cache_populator.save_variables_metadata_locally(cache_path)


cli.add_command(validate)
cli.add_command(update_cache)

if __name__ == "__main__":
    freeze_support()
    cli()

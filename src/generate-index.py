import argparse
import yaml
import result_summary
import os
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader


def load_yaml_config(config_file):
    """
    Load the YAML configuration file.

    Args:
        config_file (str): Path to the configuration file.

    Returns:
        dict: The parsed YAML data.
    """
    with open(config_file, 'r') as file:
        return yaml.safe_load(file)


def count_kernelci_nodes(html_path):
    """
    Count occurrences of "KernelCI node" in an HTML file.

    Args:
        html_path (str): Path to the HTML file.

    Returns:
        int: The number of "KernelCI node" occurrences.
    """
    try:
        with open(html_path, 'r', encoding='utf-8') as html_file:
            soup = BeautifulSoup(html_file, 'html.parser')
            return str(soup).count('KernelCI node')
    except Exception as e:
        print(f"Error reading {html_path}: {e}")
        return None


def collect_monitor_reports(reports_folder, summaries):
    """
    Collect report information from the monitors defined in the YAML config.

    Args:
        reports_folder (str): Base folder containing the reports.
        summaries (dict): Summaries definitions from the YAML config.

    Returns:
        list: A list of dictionaries with monitor report data.
    """
    dirs = []
    for _, attributes in summaries.items():
        path = attributes['metadata']['output_file']
        title = attributes['metadata']['title']
        html_path = os.path.join(reports_folder, path)
        num_reports = count_kernelci_nodes(html_path)
        if num_reports is not None:
            dirs.append({
                'path': path,
                'title': title,
                'num_reports': num_reports
            })
    return dirs


def render_template(template_file, output_file, dirs, reports_date):
    """
    Render an HTML file from a Jinja2 template.

    Args:
        template_file (str): Path to the Jinja2 template file.
        output_file (str): Path where the rendered file will be saved.
        dirs (list): List of dictionaries containing report data.
        reports_date (str): Date when the report was generated
    """
    env = Environment(loader=FileSystemLoader(result_summary.TEMPLATES_DIR))
    template = env.get_template(template_file)
    output_content = template.render(dirs=dirs, reports_date=reports_date)

    with open(output_file, 'w', encoding='utf-8') as output:
        output.write(output_content)
    print(f"{output_file} has been created.")


def main():
    parser = argparse.ArgumentParser(
        description="Script to process report data and generate an index file.")
    parser.add_argument(
        '--reports_date', help="Date when the reports were generated.")
    args = parser.parse_args()

    config_file = 'config/result-summary.yaml'
    template_file = 'gitlab_main_index.html.jinja2'
    output_file = 'data/output/index.html'
    reports_folder = "data/output/"

    # Load YAML config
    config = load_yaml_config(config_file)
    summaries = {k: v for k, v in config.items()
                 if v.get('metadata', {}).get('action') == 'summary'}

    # Collect reports listed in the YAML config
    dirs = collect_monitor_reports(reports_folder, summaries)

    # Render the final index.html file using the Jinja2 template
    render_template(template_file, output_file, dirs, args.reports_date)


if __name__ == '__main__':
    main()

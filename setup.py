from setuptools import setup


setup(
    name="otio-premiereproject",
    versioning="distance",
    author="https://github.com/splidje",
    package_data={
        'otio_premiereproject': [
            'plugin_manifest.json',
        ],
    },
    entry_points={
        'opentimelineio.plugins': 'premiereproject = otio_premiereproject'
    },
    install_requires=[
        "opentimelineio>=0.12.0",
    ],
)

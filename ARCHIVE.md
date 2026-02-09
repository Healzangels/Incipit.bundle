# Audnexus.bundle Archive Documentation

## Archive Information

- **Archive Date**: February 2026
- **Final Version**: `v1.x.x-final`
- **Branch**: `archive/legacy-python`
- **Reason**: Plex Framework 2 deprecation

## Reason for Archival

This archive contains the legacy Python implementation of Audnexus.bundle, built for Plex's Framework 2 plugin system. Plex has announced the deprecation of Framework 2, with end-of-life scheduled for 2026. This implementation is no longer actively maintained.

## Final Version Details

- **Last Release**: v1.3.2
- **Status**: Feature-complete, bug-fix only (archived)
- **Python Version**: Compatible with Plex's embedded Python environment

## Known Issues & Limitations

As of the archival date, the following known issues exist:

1. **Region Support**: Agent was built for English-based regions. Non-English regions may have limited functionality.
2. **Author Quick Match**: Authors cannot be quick matched from filenames.
3. **Year Field**: Cannot be used by music agents (Plex limitation).
4. **Collections**: Agent cannot create collections for book series.
5. **Performance**: Initial library scans may be slow for large libraries.

## Migration Path

Users are encouraged to migrate to the new Go-based implementation:

- **New Repository**: [audnexus-go](https://github.com/djdembeck/audnexus-go)
- **Architecture**: Go HTTP Metadata Provider
- **Benefits**: Better performance, modern architecture, active development

### Migration Steps

1. Remove the existing Audnexus.bundle from Plex's Plug-ins directory
2. Install the new Go-based metadata provider
3. Refresh library metadata to pull fresh data

## Archived Files

This archive preserves the following:

- All Python agent code (`Contents/`)
- README documentation
- Configuration files
- Test suites (if applicable)

## No Further Development

**This branch will not receive any further updates.**

- No new features
- No bug fixes
- No security patches

For any issues or questions, please refer to the new Go implementation.

---

*Archived on: February 2026*
*Branch: archive/legacy-python*
*Tag: v1.x.x-final*
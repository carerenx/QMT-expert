"""User constraint, 2026-10-27: Redis QMT is the sole research data source."""


def require_redis_membership(universe):
    if (universe.get('source') != 'Redis QMT'
            or universe.get('current_only', False)
            or universe.get('historical_membership_verified') is not True
            or not universe.get('snapshots')):
        raise ValueError(
            'Redis QMT only: obtain verified historical membership from Redis QMT; '
            'external reconstructed or current-only membership cannot drive this research')

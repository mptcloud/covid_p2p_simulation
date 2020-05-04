"""Entrypoint that can be used to start many inference workers on a single node."""

import covid19sim.server_bootstrap


def main(args=None):
    return covid19sim.server_bootstrap.main(args)


if __name__ == "__main__":
    main()
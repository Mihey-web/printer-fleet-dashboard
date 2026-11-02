# License

Kuznitsa is **dual-licensed**.

## Open source: GNU AGPL-3.0

The code is free software under the
[GNU Affero General Public License v3.0](https://github.com/mihey-web/printer-fleet-dashboard/blob/main/LICENSE).
In plain words:

- you may use, study, change and share it, also at a business such as a print farm;
- if you share it or a changed copy, you share it under the same license, with the source code;
- **network use counts:** if you run a **modified** version as a service that other people use
  over the network, you must offer those users the complete source code of your modified version.

Running the unmodified dashboard for your own farm is the normal open-source use.

## Commercial license

A separate commercial license is available if you want to build the dashboard into a
**closed-source** product or a hosted / SaaS offering without publishing your changes under the
AGPL, or if you otherwise cannot follow the AGPL's source-disclosure rules. It removes the
copyleft obligations under separately agreed terms.

To get one, contact the copyright holder as described in
[COMMERCIAL-LICENSE.md](https://github.com/mihey-web/printer-fleet-dashboard/blob/main/COMMERCIAL-LICENSE.md),
with a short description of how you intend to use it.

## Contributions

Contributions are accepted under the Contributor License Agreement in
[CONTRIBUTING.md](https://github.com/mihey-web/printer-fleet-dashboard/blob/main/CONTRIBUTING.md):
contributors grant the copyright holder a license that keeps the dual-licensing possible.

## Third-party code

`app/vendor/pybambu/` is a copy of the `pybambu` library from
[ha-bambulab](https://github.com/greghesp/ha-bambulab), © 2023 ha-bambulab contributors, under the
**MIT License**. It stays under the MIT License and is not part of either license above.

Bambu's error texts and button tables that the dashboard downloads at run time are Bambu's data;
they are stored only in the local `data/hms/` folder and are not part of the repository.

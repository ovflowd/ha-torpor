# Setup

Installing the integration, pairing a PC, re-pairing it and removing it. Every code, fingerprint,
address and name on this page is synthetic.

## Install

Torpor isn't in the Marketplace catalog yet, so you add it by link:

1. Select the **Open Marketplace repository** badge in the [README](../README.md), or in the
   Marketplace add `https://github.com/ovflowd/ha-torpor` as a custom repository of type
   **Integration**.
2. Download **Torpor**.
3. Restart Home Assistant if the Marketplace asks you to.

Updates come through the Marketplace. A repository added by link is checked for new releases about
every 48 hours, and only while the Marketplace has a GitHub connection.

To install by hand instead, copy `custom_components/torpor` into the `custom_components` folder of
your Home Assistant configuration and restart.

## Pair a PC

Install the agent on the PC first, as its
[quick start](https://github.com/ovflowd/torpor/blob/main/docs/quick-start.md) explains. Pairing
works only when the PC and Home Assistant are on the same local network.

1. In Home Assistant, go to **Settings > Devices & services > Add integration** and pick **Torpor**.
2. Select **Submit**. Home Assistant shows a one-time code such as
   `trp1-mp5s-7ncd-o7w2-vtka-jvzm-zay3-hmxu-zzv4`, and the command to run.
3. On the PC, run the command, for example `torporctl pair --url http://192.168.1.10:8123`, and
   type the code when it asks. It never takes the code as an argument, so it stays out of your
   shell history.
4. Both sides now show the PC's host name and a fingerprint such as `TGVU-FESI`. If they match,
   select **This is my PC**. If they don't, someone else used the code: select **Cancel**.
5. Pick the features you want ([options](options.md)), and select **Submit**.

The PC stores its key only at this last step, and Home Assistant creates the entry only after the PC
confirms it stored it. If you close the dialog, cancel, or wait more than 15 minutes, neither side
keeps anything. The code works once; after three wrong attempts it stops working.

## Re-pair

For a reinstalled PC, a lost key, or to replace the key: select **Reconfigure** on the PC's entry,
then run `torporctl pair` on the PC again. The old key stops working when you finish.

If only Home Assistant's address changed, you don't need to re-pair: run
`torporctl config set ha-url http://192.168.1.20:8123` on the PC, and the key keeps working.

## Remove

1. In Home Assistant, delete the PC's entry under **Settings > Devices & services > Torpor**. Its
   key, entities, stored settings and repairs go with it.
2. On the PC, run `torporctl uninstall --purge` to remove the agent and its key. Without `--purge`
   the agent's settings stay, and a running agent backs off quietly after Home Assistant stops
   knowing it.
3. Remove the integration from the Marketplace, or delete `custom_components/torpor`, and restart.

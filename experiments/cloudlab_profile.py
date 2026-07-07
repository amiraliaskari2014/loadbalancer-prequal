#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
CloudLab profile: one physical backend node per backend server.

This provisions 20 bare-metal backend hosts plus one load-balancer node, one
background-load node, and one monitor node. It intentionally does not run any
startup commands; setup is still done later by prepare.py over SSH.

Node layout:
  bhost1..bhost20  10.10.1.1..20   one backend server node each
  lb               10.10.1.254      load balancers + hey + scripts
  bgload           10.10.1.253      antagonist
  monitor          10.10.1.252      Prometheus + Grafana

Run with:
  bash experiment1.sh <user>@<lb-hostname>
  bash experiment2.sh <user>@<lb-hostname>
  bash experiment3.sh <user>@<lb-hostname>

Because prepare.py discovers bhost* nodes and TOTAL=20, it will place exactly
one backend container on each backend node.
"""

import geni.portal as portal
import geni.rspec.pg as rspec

IMAGE = "urn:publicid:IDN+emulab.net+image+emulab-ops//UBUNTU22-64-STD"
NET = "10.10.1."
MASK = "255.255.255.0"
BACKEND_SERVERS = 20

pc = portal.Context()
pc.defineParameter("hwType", "Hardware type for all nodes",
                   portal.ParameterType.STRING, "d430")
params = pc.bindParameters()
pc.verifyParameters()

request = pc.makeRequestRSpec()
lan = request.LAN("lan")
lan.best_effort = True
lan.vlan_tagging = True


def add_node(name, ip):
    node = request.RawPC(name)
    node.hardware_type = params.hwType
    node.disk_image = IMAGE
    iface = node.addInterface("if1")
    iface.addAddress(rspec.IPv4Address(ip, MASK))
    lan.addInterface(iface)
    return node


for i in range(1, BACKEND_SERVERS + 1):
    add_node("bhost%d" % i, NET + str(i))

add_node("lb", NET + "254")
add_node("bgload", NET + "253")
add_node("monitor", NET + "252")

pc.printRequestRSpec(request)

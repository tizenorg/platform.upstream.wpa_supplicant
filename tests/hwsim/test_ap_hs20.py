# Hotspot 2.0 tests
# Copyright (c) 2013-2015, Jouni Malinen <j@w1.fi>
#
# This software may be distributed under the terms of the BSD license.
# See README for more details.

import binascii
import struct
import time
import subprocess
import logging
logger = logging.getLogger()
import os
import os.path
import socket
import subprocess

import hostapd
from utils import HwsimSkip
import hwsim_utils
from wlantest import Wlantest
from wpasupplicant import WpaSupplicant
from test_ap_eap import check_eap_capa, check_domain_match_full

def hs20_ap_params(ssid="test-hs20"):
    params = hostapd.wpa2_params(ssid=ssid)
    params['wpa_key_mgmt'] = "WPA-EAP"
    params['ieee80211w'] = "1"
    params['ieee8021x'] = "1"
    params['auth_server_addr'] = "127.0.0.1"
    params['auth_server_port'] = "1812"
    params['auth_server_shared_secret'] = "radius"
    params['interworking'] = "1"
    params['access_network_type'] = "14"
    params['internet'] = "1"
    params['asra'] = "0"
    params['esr'] = "0"
    params['uesa'] = "0"
    params['venue_group'] = "7"
    params['venue_type'] = "1"
    params['venue_name'] = [ "eng:Example venue", "fin:Esimerkkipaikka" ]
    params['roaming_consortium'] = [ "112233", "1020304050", "010203040506",
                                     "fedcba" ]
    params['domain_name'] = "example.com,another.example.com"
    params['nai_realm'] = [ "0,example.com,13[5:6],21[2:4][5:7]",
                            "0,another.example.com" ]
    params['hs20'] = "1"
    params['hs20_wan_metrics'] = "01:8000:1000:80:240:3000"
    params['hs20_conn_capab'] = [ "1:0:2", "6:22:1", "17:5060:0" ]
    params['hs20_operating_class'] = "5173"
    params['anqp_3gpp_cell_net'] = "244,91"
    return params

def check_auto_select(dev, bssid):
    dev.scan_for_bss(bssid, freq="2412")
    dev.request("INTERWORKING_SELECT auto freq=2412")
    ev = dev.wait_connected(timeout=15)
    if bssid not in ev:
        raise Exception("Connected to incorrect network")
    dev.request("REMOVE_NETWORK all")
    dev.wait_disconnected()

def interworking_select(dev, bssid, type=None, no_match=False, freq=None):
    dev.dump_monitor()
    if bssid and freq and not no_match:
        dev.scan_for_bss(bssid, freq=freq)
    freq_extra = " freq=" + freq if freq else ""
    dev.request("INTERWORKING_SELECT" + freq_extra)
    ev = dev.wait_event(["INTERWORKING-AP", "INTERWORKING-NO-MATCH"],
                        timeout=15)
    if ev is None:
        raise Exception("Network selection timed out");
    if no_match:
        if "INTERWORKING-NO-MATCH" not in ev:
            raise Exception("Unexpected network match")
        return
    if "INTERWORKING-NO-MATCH" in ev:
        logger.info("Matching network not found - try again")
        dev.dump_monitor()
        dev.request("INTERWORKING_SELECT" + freq_extra)
        ev = dev.wait_event(["INTERWORKING-AP", "INTERWORKING-NO-MATCH"],
                            timeout=15)
        if ev is None:
            raise Exception("Network selection timed out");
        if "INTERWORKING-NO-MATCH" in ev:
            raise Exception("Matching network not found")
    if bssid and bssid not in ev:
        raise Exception("Unexpected BSSID in match")
    if type and "type=" + type not in ev:
        raise Exception("Network type not recognized correctly")

def check_sp_type(dev, sp_type):
    type = dev.get_status_field("sp_type")
    if type is None:
        raise Exception("sp_type not available")
    if type != sp_type:
        raise Exception("sp_type did not indicate home network")

def hlr_auc_gw_available():
    if not os.path.exists("/tmp/hlr_auc_gw.sock"):
        raise HwsimSkip("No hlr_auc_gw socket available")
    if not os.path.exists("../../hostapd/hlr_auc_gw"):
        raise HwsimSkip("No hlr_auc_gw available")

def interworking_ext_sim_connect(dev, bssid, method):
    dev.request("INTERWORKING_CONNECT " + bssid)
    interworking_ext_sim_auth(dev, method)

def interworking_ext_sim_auth(dev, method):
    ev = dev.wait_event(["CTRL-EVENT-EAP-METHOD"], timeout=15)
    if ev is None:
        raise Exception("Network connected timed out")
    if "(" + method + ")" not in ev:
        raise Exception("Unexpected EAP method selection")

    ev = dev.wait_event(["CTRL-REQ-SIM"], timeout=15)
    if ev is None:
        raise Exception("Wait for external SIM processing request timed out")
    p = ev.split(':', 2)
    if p[1] != "GSM-AUTH":
        raise Exception("Unexpected CTRL-REQ-SIM type")
    id = p[0].split('-')[3]
    rand = p[2].split(' ')[0]

    res = subprocess.check_output(["../../hostapd/hlr_auc_gw",
                                   "-m",
                                   "auth_serv/hlr_auc_gw.milenage_db",
                                   "GSM-AUTH-REQ 232010000000000 " + rand])
    if "GSM-AUTH-RESP" not in res:
        raise Exception("Unexpected hlr_auc_gw response")
    resp = res.split(' ')[2].rstrip()

    dev.request("CTRL-RSP-SIM-" + id + ":GSM-AUTH:" + resp)
    dev.wait_connected(timeout=15)

def interworking_connect(dev, bssid, method):
    dev.request("INTERWORKING_CONNECT " + bssid)
    interworking_auth(dev, method)

def interworking_auth(dev, method):
    ev = dev.wait_event(["CTRL-EVENT-EAP-METHOD"], timeout=15)
    if ev is None:
        raise Exception("Network connected timed out")
    if "(" + method + ")" not in ev:
        raise Exception("Unexpected EAP method selection")

    dev.wait_connected(timeout=15)

def check_probe_resp(wt, bssid_unexpected, bssid_expected):
    if bssid_unexpected:
        count = wt.get_bss_counter("probe_response", bssid_unexpected)
        if count > 0:
            raise Exception("Unexpected Probe Response frame from AP")

    if bssid_expected:
        count = wt.get_bss_counter("probe_response", bssid_expected)
        if count == 0:
            raise Exception("No Probe Response frame from AP")

def test_ap_anqp_sharing(dev, apdev):
    """ANQP sharing within ESS and explicit unshare"""
    dev[0].flush_scan_cache()

    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['hessid'] = bssid
    hostapd.add_ap(apdev[0]['ifname'], params)

    bssid2 = apdev[1]['bssid']
    params = hs20_ap_params()
    params['hessid'] = bssid
    params['nai_realm'] = [ "0,example.com,13[5:6],21[2:4][5:7]" ]
    hostapd.add_ap(apdev[1]['ifname'], params)

    dev[0].hs20_enable()
    id = dev[0].add_cred_values({ 'realm': "example.com", 'username': "test",
                                  'password': "secret",
                                  'domain': "example.com" })
    logger.info("Normal network selection with shared ANQP results")
    dev[0].scan_for_bss(bssid, freq="2412")
    dev[0].scan_for_bss(bssid2, freq="2412")
    interworking_select(dev[0], None, "home", freq="2412")
    dev[0].dump_monitor()

    logger.debug("BSS entries:\n" + dev[0].request("BSS RANGE=ALL"))
    res1 = dev[0].get_bss(bssid)
    res2 = dev[0].get_bss(bssid2)
    if 'anqp_nai_realm' not in res1:
        raise Exception("anqp_nai_realm not found for AP1")
    if 'anqp_nai_realm' not in res2:
        raise Exception("anqp_nai_realm not found for AP2")
    if res1['anqp_nai_realm'] != res2['anqp_nai_realm']:
        raise Exception("ANQP results were not shared between BSSes")

    logger.info("Explicit ANQP request to unshare ANQP results")
    dev[0].request("ANQP_GET " + bssid + " 263")
    ev = dev[0].wait_event(["RX-ANQP"], timeout=5)
    if ev is None:
        raise Exception("ANQP operation timed out")

    dev[0].request("ANQP_GET " + bssid2 + " 263")
    ev = dev[0].wait_event(["RX-ANQP"], timeout=5)
    if ev is None:
        raise Exception("ANQP operation timed out")

    res1 = dev[0].get_bss(bssid)
    res2 = dev[0].get_bss(bssid2)
    if res1['anqp_nai_realm'] == res2['anqp_nai_realm']:
        raise Exception("ANQP results were not unshared")

def test_ap_nai_home_realm_query(dev, apdev):
    """NAI Home Realm Query"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['nai_realm'] = [ "0,example.com,13[5:6],21[2:4][5:7]",
                            "0,another.example.org" ]
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].scan(freq="2412")
    dev[0].request("HS20_GET_NAI_HOME_REALM_LIST " + bssid + " realm=example.com")
    ev = dev[0].wait_event(["RX-ANQP"], timeout=5)
    if ev is None:
        raise Exception("ANQP operation timed out")
    nai1 = dev[0].get_bss(bssid)['anqp_nai_realm']
    dev[0].dump_monitor()

    dev[0].request("ANQP_GET " + bssid + " 263")
    ev = dev[0].wait_event(["RX-ANQP"], timeout=5)
    if ev is None:
        raise Exception("ANQP operation timed out")
    nai2 = dev[0].get_bss(bssid)['anqp_nai_realm']

    if len(nai1) >= len(nai2):
        raise Exception("Unexpected NAI Realm list response lengths")
    if "example.com".encode('hex') not in nai1:
        raise Exception("Home realm not reported")
    if "example.org".encode('hex') in nai1:
        raise Exception("Non-home realm reported")
    if "example.com".encode('hex') not in nai2:
        raise Exception("Home realm not reported in wildcard query")
    if "example.org".encode('hex') not in nai2:
        raise Exception("Non-home realm not reported in wildcard query ")

    cmds = [ "foo",
             "00:11:22:33:44:55 123",
             "00:11:22:33:44:55 qq" ]
    for cmd in cmds:
        if "FAIL" not in dev[0].request("HS20_GET_NAI_HOME_REALM_LIST " + cmd):
            raise Exception("Invalid HS20_GET_NAI_HOME_REALM_LIST accepted: " + cmd)

    dev[0].dump_monitor()
    if "OK" not in dev[0].request("HS20_GET_NAI_HOME_REALM_LIST " + bssid):
        raise Exception("HS20_GET_NAI_HOME_REALM_LIST failed")
    ev = dev[0].wait_event(["GAS-QUERY-DONE"], timeout=10)
    if ev is None:
        raise Exception("ANQP operation timed out")
    ev = dev[0].wait_event(["RX-ANQP"], timeout=0.1)
    if ev is not None:
        raise Exception("Unexpected ANQP response: " + ev)

    dev[0].dump_monitor()
    if "OK" not in dev[0].request("HS20_GET_NAI_HOME_REALM_LIST " + bssid + " 01000b6578616d706c652e636f6d"):
        raise Exception("HS20_GET_NAI_HOME_REALM_LIST failed")
    ev = dev[0].wait_event(["RX-ANQP"], timeout=10)
    if ev is None:
        raise Exception("No ANQP response")
    if "NAI Realm list" not in ev:
        raise Exception("Missing NAI Realm list: " + ev)

    dev[0].add_cred_values({ 'realm': "example.com", 'username': "test",
                             'password': "secret",
                             'domain': "example.com" })
    dev[0].dump_monitor()
    if "OK" not in dev[0].request("HS20_GET_NAI_HOME_REALM_LIST " + bssid):
        raise Exception("HS20_GET_NAI_HOME_REALM_LIST failed")
    ev = dev[0].wait_event(["RX-ANQP"], timeout=10)
    if ev is None:
        raise Exception("No ANQP response")
    if "NAI Realm list" not in ev:
        raise Exception("Missing NAI Realm list: " + ev)

def test_ap_interworking_scan_filtering(dev, apdev):
    """Interworking scan filtering with HESSID and access network type"""
    try:
        _test_ap_interworking_scan_filtering(dev, apdev)
    finally:
        dev[0].request("SET hessid 00:00:00:00:00:00")
        dev[0].request("SET access_network_type 15")

def _test_ap_interworking_scan_filtering(dev, apdev):
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    ssid = "test-hs20-ap1"
    params['ssid'] = ssid
    params['hessid'] = bssid
    hostapd.add_ap(apdev[0]['ifname'], params)

    bssid2 = apdev[1]['bssid']
    params = hs20_ap_params()
    ssid2 = "test-hs20-ap2"
    params['ssid'] = ssid2
    params['hessid'] = bssid2
    params['access_network_type'] = "1"
    del params['venue_group']
    del params['venue_type']
    hostapd.add_ap(apdev[1]['ifname'], params)

    dev[0].hs20_enable()

    wt = Wlantest()
    wt.flush()

    logger.info("Check probe request filtering based on HESSID")

    dev[0].request("SET hessid " + bssid2)
    dev[0].scan(freq="2412")
    time.sleep(0.03)
    check_probe_resp(wt, bssid, bssid2)

    logger.info("Check probe request filtering based on access network type")

    wt.clear_bss_counters(bssid)
    wt.clear_bss_counters(bssid2)
    dev[0].request("SET hessid 00:00:00:00:00:00")
    dev[0].request("SET access_network_type 14")
    dev[0].scan(freq="2412")
    time.sleep(0.03)
    check_probe_resp(wt, bssid2, bssid)

    wt.clear_bss_counters(bssid)
    wt.clear_bss_counters(bssid2)
    dev[0].request("SET hessid 00:00:00:00:00:00")
    dev[0].request("SET access_network_type 1")
    dev[0].scan(freq="2412")
    time.sleep(0.03)
    check_probe_resp(wt, bssid, bssid2)

    logger.info("Check probe request filtering based on HESSID and ANT")

    wt.clear_bss_counters(bssid)
    wt.clear_bss_counters(bssid2)
    dev[0].request("SET hessid " + bssid)
    dev[0].request("SET access_network_type 14")
    dev[0].scan(freq="2412")
    time.sleep(0.03)
    check_probe_resp(wt, bssid2, bssid)

    wt.clear_bss_counters(bssid)
    wt.clear_bss_counters(bssid2)
    dev[0].request("SET hessid " + bssid2)
    dev[0].request("SET access_network_type 14")
    dev[0].scan(freq="2412")
    time.sleep(0.03)
    check_probe_resp(wt, bssid, None)
    check_probe_resp(wt, bssid2, None)

    wt.clear_bss_counters(bssid)
    wt.clear_bss_counters(bssid2)
    dev[0].request("SET hessid " + bssid)
    dev[0].request("SET access_network_type 1")
    dev[0].scan(freq="2412")
    time.sleep(0.03)
    check_probe_resp(wt, bssid, None)
    check_probe_resp(wt, bssid2, None)

def test_ap_hs20_select(dev, apdev):
    """Hotspot 2.0 network selection"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['hessid'] = bssid
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    id = dev[0].add_cred_values({ 'realm': "example.com", 'username': "test",
                                  'password': "secret",
                                  'domain': "example.com" })
    interworking_select(dev[0], bssid, "home")

    dev[0].remove_cred(id)
    id = dev[0].add_cred_values({ 'realm': "example.com", 'username': "test",
                                  'password': "secret",
                                  'domain': "no.match.example.com" })
    interworking_select(dev[0], bssid, "roaming", freq="2412")

    dev[0].set_cred_quoted(id, "realm", "no.match.example.com");
    interworking_select(dev[0], bssid, no_match=True, freq="2412")

    res = dev[0].request("SCAN_RESULTS")
    if "[HS20]" not in res:
        raise Exception("HS20 flag missing from scan results: " + res)

    bssid2 = apdev[1]['bssid']
    params = hs20_ap_params()
    params['nai_realm'] = [ "0,example.org,21" ]
    params['hessid'] = bssid2
    params['domain_name'] = "example.org"
    hostapd.add_ap(apdev[1]['ifname'], params)
    dev[0].remove_cred(id)
    id = dev[0].add_cred_values({ 'realm': "example.org", 'username': "test",
                                  'password': "secret",
                                  'domain': "example.org" })
    interworking_select(dev[0], bssid2, "home", freq="2412")

def hs20_simulated_sim(dev, ap, method):
    bssid = ap['bssid']
    params = hs20_ap_params()
    params['hessid'] = bssid
    params['anqp_3gpp_cell_net'] = "555,444"
    params['domain_name'] = "wlan.mnc444.mcc555.3gppnetwork.org"
    hostapd.add_ap(ap['ifname'], params)

    dev.hs20_enable()
    dev.add_cred_values({ 'imsi': "555444-333222111", 'eap': method,
                          'milenage': "5122250214c33e723a5dd523fc145fc0:981d464c7c52eb6e5036234984ad0bcf:000000000123"})
    interworking_select(dev, "home", freq="2412")
    interworking_connect(dev, bssid, method)
    check_sp_type(dev, "home")

def test_ap_hs20_sim(dev, apdev):
    """Hotspot 2.0 with simulated SIM and EAP-SIM"""
    hlr_auc_gw_available()
    hs20_simulated_sim(dev[0], apdev[0], "SIM")
    dev[0].request("INTERWORKING_SELECT auto freq=2412")
    ev = dev[0].wait_event(["INTERWORKING-ALREADY-CONNECTED"], timeout=15)
    if ev is None:
        raise Exception("Timeout on already-connected event")

def test_ap_hs20_aka(dev, apdev):
    """Hotspot 2.0 with simulated USIM and EAP-AKA"""
    hlr_auc_gw_available()
    hs20_simulated_sim(dev[0], apdev[0], "AKA")

def test_ap_hs20_aka_prime(dev, apdev):
    """Hotspot 2.0 with simulated USIM and EAP-AKA'"""
    hlr_auc_gw_available()
    hs20_simulated_sim(dev[0], apdev[0], "AKA'")

def test_ap_hs20_ext_sim(dev, apdev):
    """Hotspot 2.0 with external SIM processing"""
    hlr_auc_gw_available()
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['hessid'] = bssid
    params['anqp_3gpp_cell_net'] = "232,01"
    params['domain_name'] = "wlan.mnc001.mcc232.3gppnetwork.org"
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    try:
        dev[0].request("SET external_sim 1")
        dev[0].add_cred_values({ 'imsi': "23201-0000000000", 'eap': "SIM" })
        interworking_select(dev[0], "home", freq="2412")
        interworking_ext_sim_connect(dev[0], bssid, "SIM")
        check_sp_type(dev[0], "home")
    finally:
        dev[0].request("SET external_sim 0")

def test_ap_hs20_ext_sim_roaming(dev, apdev):
    """Hotspot 2.0 with external SIM processing in roaming network"""
    hlr_auc_gw_available()
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['hessid'] = bssid
    params['anqp_3gpp_cell_net'] = "244,91;310,026;232,01;234,56"
    params['domain_name'] = "wlan.mnc091.mcc244.3gppnetwork.org"
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    try:
        dev[0].request("SET external_sim 1")
        dev[0].add_cred_values({ 'imsi': "23201-0000000000", 'eap': "SIM" })
        interworking_select(dev[0], "roaming", freq="2412")
        interworking_ext_sim_connect(dev[0], bssid, "SIM")
        check_sp_type(dev[0], "roaming")
    finally:
        dev[0].request("SET external_sim 0")

def test_ap_hs20_username(dev, apdev):
    """Hotspot 2.0 connection in username/password credential"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['hessid'] = bssid
    params['disable_dgaf'] = '1'
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    id = dev[0].add_cred_values({ 'realm': "example.com",
                                  'username': "hs20-test",
                                  'password': "password",
                                  'ca_cert': "auth_serv/ca.pem",
                                  'domain': "example.com",
                                  'update_identifier': "1234" })
    interworking_select(dev[0], bssid, "home", freq="2412")
    interworking_connect(dev[0], bssid, "TTLS")
    check_sp_type(dev[0], "home")
    status = dev[0].get_status()
    if status['pairwise_cipher'] != "CCMP":
        raise Exception("Unexpected pairwise cipher")
    if status['hs20'] != "2":
        raise Exception("Unexpected HS 2.0 support indication")

    dev[1].connect("test-hs20", key_mgmt="WPA-EAP", eap="TTLS",
                   identity="hs20-test", password="password",
                   ca_cert="auth_serv/ca.pem", phase2="auth=MSCHAPV2",
                   scan_freq="2412")

def test_ap_hs20_connect_api(dev, apdev):
    """Hotspot 2.0 connection with connect API"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['hessid'] = bssid
    params['disable_dgaf'] = '1'
    hostapd.add_ap(apdev[0]['ifname'], params)

    wpas = WpaSupplicant(global_iface='/tmp/wpas-wlan5')
    wpas.interface_add("wlan5", drv_params="force_connect_cmd=1")
    wpas.hs20_enable()
    wpas.flush_scan_cache()
    id = wpas.add_cred_values({ 'realm': "example.com",
                                  'username': "hs20-test",
                                  'password': "password",
                                  'ca_cert': "auth_serv/ca.pem",
                                  'domain': "example.com",
                                  'update_identifier': "1234" })
    interworking_select(wpas, bssid, "home", freq="2412")
    interworking_connect(wpas, bssid, "TTLS")
    check_sp_type(wpas, "home")
    status = wpas.get_status()
    if status['pairwise_cipher'] != "CCMP":
        raise Exception("Unexpected pairwise cipher")
    if status['hs20'] != "2":
        raise Exception("Unexpected HS 2.0 support indication")

def test_ap_hs20_auto_interworking(dev, apdev):
    """Hotspot 2.0 connection with auto_interworking=1"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['hessid'] = bssid
    params['disable_dgaf'] = '1'
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable(auto_interworking=True)
    id = dev[0].add_cred_values({ 'realm': "example.com",
                                  'username': "hs20-test",
                                  'password': "password",
                                  'ca_cert': "auth_serv/ca.pem",
                                  'domain': "example.com",
                                  'update_identifier': "1234" })
    dev[0].request("REASSOCIATE")
    dev[0].wait_connected(timeout=15)
    check_sp_type(dev[0], "home")
    status = dev[0].get_status()
    if status['pairwise_cipher'] != "CCMP":
        raise Exception("Unexpected pairwise cipher")
    if status['hs20'] != "2":
        raise Exception("Unexpected HS 2.0 support indication")

def test_ap_hs20_auto_interworking_no_match(dev, apdev):
    """Hotspot 2.0 connection with auto_interworking=1 and no matching network"""
    hapd = hostapd.add_ap(apdev[0]['ifname'], { "ssid": "mismatch" })

    dev[0].hs20_enable(auto_interworking=True)
    id = dev[0].connect("mismatch", psk="12345678", scan_freq="2412",
                        only_add_network=True)
    dev[0].request("ENABLE_NETWORK " + str(id) + " no-connect")

    id = dev[0].add_cred_values({ 'realm': "example.com",
                                  'username': "hs20-test",
                                  'password': "password",
                                  'ca_cert': "auth_serv/ca.pem",
                                  'domain': "example.com",
                                  'update_identifier': "1234" })
    dev[0].request("INTERWORKING_SELECT auto freq=2412")
    time.sleep(0.1)
    dev[0].dump_monitor()
    for i in range(5):
        logger.info("start ping")
        if "PONG" not in dev[0].ctrl.request("PING", timeout=2):
            raise Exception("PING failed")
        logger.info("ping done")
        fetch = 0
        scan = 0
        for j in range(15):
            ev = dev[0].wait_event([ "ANQP fetch completed",
                                     "CTRL-EVENT-SCAN-RESULTS" ], timeout=0.05)
            if ev is None:
                break
            if "ANQP fetch completed" in ev:
                fetch += 1
            else:
                scan += 1
        if fetch > 2 * scan + 3:
            raise Exception("Too many ANQP fetch iterations")
        dev[0].dump_monitor()
    dev[0].request("DISCONNECT")

def test_ap_hs20_auto_interworking_no_cred_match(dev, apdev):
    """Hotspot 2.0 connection with auto_interworking=1 but no cred match"""
    bssid = apdev[0]['bssid']
    params = { "ssid": "test" }
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable(auto_interworking=True)
    dev[0].add_cred_values({ 'realm': "example.com",
                             'username': "hs20-test",
                             'password': "password",
                             'ca_cert': "auth_serv/ca.pem",
                             'domain': "example.com" })

    id = dev[0].connect("test", psk="12345678", only_add_network=True)
    dev[0].request("ENABLE_NETWORK %s" % id)
    logger.info("Verify that scanning continues when there is partial network block match")
    for i in range(0, 2):
        ev = dev[0].wait_event(["CTRL-EVENT-SCAN-RESULTS"], 10)
        if ev is None:
            raise Exception("Scan timed out")
        logger.info("Scan completed")

def eap_test(dev, ap, eap_params, method, user):
    bssid = ap['bssid']
    params = hs20_ap_params()
    params['nai_realm'] = [ "0,example.com," + eap_params ]
    hostapd.add_ap(ap['ifname'], params)

    dev.hs20_enable()
    dev.add_cred_values({ 'realm': "example.com",
                          'ca_cert': "auth_serv/ca.pem",
                          'username': user,
                          'password': "password" })
    interworking_select(dev, bssid, freq="2412")
    interworking_connect(dev, bssid, method)

def test_ap_hs20_eap_unknown(dev, apdev):
    """Hotspot 2.0 connection with unknown EAP method"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['nai_realm'] = "0,example.com,99"
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    dev[0].add_cred_values(default_cred())
    interworking_select(dev[0], None, no_match=True, freq="2412")

def test_ap_hs20_eap_peap_mschapv2(dev, apdev):
    """Hotspot 2.0 connection with PEAP/MSCHAPV2"""
    eap_test(dev[0], apdev[0], "25[3:26]", "PEAP", "user")

def test_ap_hs20_eap_peap_default(dev, apdev):
    """Hotspot 2.0 connection with PEAP/MSCHAPV2 (as default)"""
    eap_test(dev[0], apdev[0], "25", "PEAP", "user")

def test_ap_hs20_eap_peap_gtc(dev, apdev):
    """Hotspot 2.0 connection with PEAP/GTC"""
    eap_test(dev[0], apdev[0], "25[3:6]", "PEAP", "user")

def test_ap_hs20_eap_peap_unknown(dev, apdev):
    """Hotspot 2.0 connection with PEAP/unknown"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['nai_realm'] = "0,example.com,25[3:99]"
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    dev[0].add_cred_values(default_cred())
    interworking_select(dev[0], None, no_match=True, freq="2412")

def test_ap_hs20_eap_ttls_chap(dev, apdev):
    """Hotspot 2.0 connection with TTLS/CHAP"""
    eap_test(dev[0], apdev[0], "21[2:2]", "TTLS", "chap user")

def test_ap_hs20_eap_ttls_mschap(dev, apdev):
    """Hotspot 2.0 connection with TTLS/MSCHAP"""
    eap_test(dev[0], apdev[0], "21[2:3]", "TTLS", "mschap user")

def test_ap_hs20_eap_ttls_eap_mschapv2(dev, apdev):
    """Hotspot 2.0 connection with TTLS/EAP-MSCHAPv2"""
    eap_test(dev[0], apdev[0], "21[3:26][6:7][99:99]", "TTLS", "user")

def test_ap_hs20_eap_ttls_eap_unknown(dev, apdev):
    """Hotspot 2.0 connection with TTLS/EAP-unknown"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['nai_realm'] = "0,example.com,21[3:99]"
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    dev[0].add_cred_values(default_cred())
    interworking_select(dev[0], None, no_match=True, freq="2412")

def test_ap_hs20_eap_ttls_eap_unsupported(dev, apdev):
    """Hotspot 2.0 connection with TTLS/EAP-OTP(unsupported)"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['nai_realm'] = "0,example.com,21[3:5]"
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    dev[0].add_cred_values(default_cred())
    interworking_select(dev[0], None, no_match=True, freq="2412")

def test_ap_hs20_eap_ttls_unknown(dev, apdev):
    """Hotspot 2.0 connection with TTLS/unknown"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['nai_realm'] = "0,example.com,21[2:5]"
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    dev[0].add_cred_values(default_cred())
    interworking_select(dev[0], None, no_match=True, freq="2412")

def test_ap_hs20_eap_fast_mschapv2(dev, apdev):
    """Hotspot 2.0 connection with FAST/EAP-MSCHAPV2"""
    check_eap_capa(dev[0], "FAST")
    eap_test(dev[0], apdev[0], "43[3:26]", "FAST", "user")

def test_ap_hs20_eap_fast_gtc(dev, apdev):
    """Hotspot 2.0 connection with FAST/EAP-GTC"""
    check_eap_capa(dev[0], "FAST")
    eap_test(dev[0], apdev[0], "43[3:6]", "FAST", "user")

def test_ap_hs20_eap_tls(dev, apdev):
    """Hotspot 2.0 connection with EAP-TLS"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['nai_realm'] = [ "0,example.com,13[5:6]" ]
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    dev[0].add_cred_values({ 'realm': "example.com",
                             'username': "certificate-user",
                             'ca_cert': "auth_serv/ca.pem",
                             'client_cert': "auth_serv/user.pem",
                             'private_key': "auth_serv/user.key"})
    interworking_select(dev[0], bssid, freq="2412")
    interworking_connect(dev[0], bssid, "TLS")

def test_ap_hs20_eap_cert_unknown(dev, apdev):
    """Hotspot 2.0 connection with certificate, but unknown EAP method"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['nai_realm'] = [ "0,example.com,99[5:6]" ]
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    dev[0].add_cred_values({ 'realm': "example.com",
                             'username': "certificate-user",
                             'ca_cert': "auth_serv/ca.pem",
                             'client_cert': "auth_serv/user.pem",
                             'private_key': "auth_serv/user.key"})
    interworking_select(dev[0], None, no_match=True, freq="2412")

def test_ap_hs20_eap_cert_unsupported(dev, apdev):
    """Hotspot 2.0 connection with certificate, but unsupported TTLS"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['nai_realm'] = [ "0,example.com,21[5:6]" ]
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    dev[0].add_cred_values({ 'realm': "example.com",
                             'username': "certificate-user",
                             'ca_cert': "auth_serv/ca.pem",
                             'client_cert': "auth_serv/user.pem",
                             'private_key': "auth_serv/user.key"})
    interworking_select(dev[0], None, no_match=True, freq="2412")

def test_ap_hs20_eap_invalid_cred(dev, apdev):
    """Hotspot 2.0 connection with invalid cred configuration"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    dev[0].add_cred_values({ 'realm': "example.com",
                             'username': "certificate-user",
                             'client_cert': "auth_serv/user.pem" })
    interworking_select(dev[0], None, no_match=True, freq="2412")

def test_ap_hs20_nai_realms(dev, apdev):
    """Hotspot 2.0 connection and multiple NAI realms and TTLS/PAP"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['hessid'] = bssid
    params['nai_realm'] = [ "0,no.match.here;example.com;no.match.here.either,21[2:1][5:7]" ]
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    id = dev[0].add_cred_values({ 'realm': "example.com",
                                  'ca_cert': "auth_serv/ca.pem",
                                  'username': "pap user",
                                  'password': "password",
                                  'domain': "example.com" })
    interworking_select(dev[0], bssid, "home", freq="2412")
    interworking_connect(dev[0], bssid, "TTLS")
    check_sp_type(dev[0], "home")

def test_ap_hs20_roaming_consortium(dev, apdev):
    """Hotspot 2.0 connection based on roaming consortium match"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['hessid'] = bssid
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    for consortium in [ "112233", "1020304050", "010203040506", "fedcba" ]:
        id = dev[0].add_cred_values({ 'username': "user",
                                      'password': "password",
                                      'domain': "example.com",
                                      'ca_cert': "auth_serv/ca.pem",
                                      'roaming_consortium': consortium,
                                      'eap': "PEAP" })
        interworking_select(dev[0], bssid, "home", freq="2412")
        interworking_connect(dev[0], bssid, "PEAP")
        check_sp_type(dev[0], "home")
        dev[0].request("INTERWORKING_SELECT auto freq=2412")
        ev = dev[0].wait_event(["INTERWORKING-ALREADY-CONNECTED"], timeout=15)
        if ev is None:
            raise Exception("Timeout on already-connected event")
        dev[0].remove_cred(id)

def test_ap_hs20_username_roaming(dev, apdev):
    """Hotspot 2.0 connection in username/password credential (roaming)"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['nai_realm'] = [ "0,example.com,13[5:6],21[2:4][5:7]",
                            "0,roaming.example.com,21[2:4][5:7]",
                            "0,another.example.com" ]
    params['domain_name'] = "another.example.com"
    params['hessid'] = bssid
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    id = dev[0].add_cred_values({ 'realm': "roaming.example.com",
                                  'username': "hs20-test",
                                  'password': "password",
                                  'ca_cert': "auth_serv/ca.pem",
                                  'domain': "example.com" })
    interworking_select(dev[0], bssid, "roaming", freq="2412")
    interworking_connect(dev[0], bssid, "TTLS")
    check_sp_type(dev[0], "roaming")

def test_ap_hs20_username_unknown(dev, apdev):
    """Hotspot 2.0 connection in username/password credential (no domain in cred)"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['hessid'] = bssid
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    id = dev[0].add_cred_values({ 'realm': "example.com",
                                  'ca_cert': "auth_serv/ca.pem",
                                  'username': "hs20-test",
                                  'password': "password" })
    interworking_select(dev[0], bssid, "unknown", freq="2412")
    interworking_connect(dev[0], bssid, "TTLS")
    check_sp_type(dev[0], "unknown")

def test_ap_hs20_username_unknown2(dev, apdev):
    """Hotspot 2.0 connection in username/password credential (no domain advertized)"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['hessid'] = bssid
    del params['domain_name']
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    id = dev[0].add_cred_values({ 'realm': "example.com",
                                  'ca_cert': "auth_serv/ca.pem",
                                  'username': "hs20-test",
                                  'password': "password",
                                  'domain': "example.com" })
    interworking_select(dev[0], bssid, "unknown", freq="2412")
    interworking_connect(dev[0], bssid, "TTLS")
    check_sp_type(dev[0], "unknown")

def test_ap_hs20_gas_while_associated(dev, apdev):
    """Hotspot 2.0 connection with GAS query while associated"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['hessid'] = bssid
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    id = dev[0].add_cred_values({ 'realm': "example.com",
                                  'ca_cert': "auth_serv/ca.pem",
                                  'username': "hs20-test",
                                  'password': "password",
                                  'domain': "example.com" })
    interworking_select(dev[0], bssid, "home", freq="2412")
    interworking_connect(dev[0], bssid, "TTLS")

    logger.info("Verifying GAS query while associated")
    dev[0].request("FETCH_ANQP")
    for i in range(0, 6):
        ev = dev[0].wait_event(["RX-ANQP"], timeout=5)
        if ev is None:
            raise Exception("Operation timed out")

def test_ap_hs20_gas_while_associated_with_pmf(dev, apdev):
    """Hotspot 2.0 connection with GAS query while associated and using PMF"""
    try:
        _test_ap_hs20_gas_while_associated_with_pmf(dev, apdev)
    finally:
        dev[0].request("SET pmf 0")

def _test_ap_hs20_gas_while_associated_with_pmf(dev, apdev):
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['hessid'] = bssid
    hostapd.add_ap(apdev[0]['ifname'], params)

    bssid2 = apdev[1]['bssid']
    params = hs20_ap_params()
    params['hessid'] = bssid2
    params['nai_realm'] = [ "0,no-match.example.org,13[5:6],21[2:4][5:7]" ]
    hostapd.add_ap(apdev[1]['ifname'], params)

    dev[0].hs20_enable()
    dev[0].request("SET pmf 2")
    id = dev[0].add_cred_values({ 'realm': "example.com",
                                  'ca_cert': "auth_serv/ca.pem",
                                  'username': "hs20-test",
                                  'password': "password",
                                  'domain': "example.com" })
    interworking_select(dev[0], bssid, "home", freq="2412")
    interworking_connect(dev[0], bssid, "TTLS")

    logger.info("Verifying GAS query while associated")
    dev[0].request("FETCH_ANQP")
    for i in range(0, 2 * 6):
        ev = dev[0].wait_event(["RX-ANQP"], timeout=5)
        if ev is None:
            raise Exception("Operation timed out")

def test_ap_hs20_gas_frag_while_associated(dev, apdev):
    """Hotspot 2.0 connection with fragmented GAS query while associated"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['hessid'] = bssid
    hostapd.add_ap(apdev[0]['ifname'], params)
    hapd = hostapd.Hostapd(apdev[0]['ifname'])
    hapd.set("gas_frag_limit", "50")

    dev[0].hs20_enable()
    id = dev[0].add_cred_values({ 'realm': "example.com",
                                  'ca_cert': "auth_serv/ca.pem",
                                  'username': "hs20-test",
                                  'password': "password",
                                  'domain': "example.com" })
    interworking_select(dev[0], bssid, "home", freq="2412")
    interworking_connect(dev[0], bssid, "TTLS")

    logger.info("Verifying GAS query while associated")
    dev[0].request("FETCH_ANQP")
    for i in range(0, 6):
        ev = dev[0].wait_event(["RX-ANQP"], timeout=5)
        if ev is None:
            raise Exception("Operation timed out")

def test_ap_hs20_multiple_connects(dev, apdev):
    """Hotspot 2.0 connection through multiple network selections"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['hessid'] = bssid
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    values = { 'realm': "example.com",
               'ca_cert': "auth_serv/ca.pem",
               'username': "hs20-test",
               'password': "password",
               'domain': "example.com" }
    id = dev[0].add_cred_values(values)

    dev[0].scan_for_bss(bssid, freq="2412")

    for i in range(0, 3):
        logger.info("Starting Interworking network selection")
        dev[0].request("INTERWORKING_SELECT auto freq=2412")
        while True:
            ev = dev[0].wait_event(["INTERWORKING-NO-MATCH",
                                    "INTERWORKING-ALREADY-CONNECTED",
                                    "CTRL-EVENT-CONNECTED"], timeout=15)
            if ev is None:
                raise Exception("Connection timed out")
            if "INTERWORKING-NO-MATCH" in ev:
                raise Exception("Matching AP not found")
            if "CTRL-EVENT-CONNECTED" in ev:
                break
            if i == 2 and "INTERWORKING-ALREADY-CONNECTED" in ev:
                break
        if i == 0:
            dev[0].request("DISCONNECT")
        dev[0].dump_monitor()

    networks = dev[0].list_networks()
    if len(networks) > 1:
        raise Exception("Duplicated network block detected")

def test_ap_hs20_disallow_aps(dev, apdev):
    """Hotspot 2.0 connection and disallow_aps"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['hessid'] = bssid
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    values = { 'realm': "example.com",
               'ca_cert': "auth_serv/ca.pem",
               'username': "hs20-test",
               'password': "password",
               'domain': "example.com" }
    id = dev[0].add_cred_values(values)

    dev[0].scan_for_bss(bssid, freq="2412")

    logger.info("Verify disallow_aps bssid")
    dev[0].request("SET disallow_aps bssid " + bssid.translate(None, ':'))
    dev[0].request("INTERWORKING_SELECT auto")
    ev = dev[0].wait_event(["INTERWORKING-NO-MATCH"], timeout=15)
    if ev is None:
        raise Exception("Network selection timed out")
    dev[0].dump_monitor()

    logger.info("Verify disallow_aps ssid")
    dev[0].request("SET disallow_aps ssid 746573742d68733230")
    dev[0].request("INTERWORKING_SELECT auto freq=2412")
    ev = dev[0].wait_event(["INTERWORKING-NO-MATCH"], timeout=15)
    if ev is None:
        raise Exception("Network selection timed out")
    dev[0].dump_monitor()

    logger.info("Verify disallow_aps clear")
    dev[0].request("SET disallow_aps ")
    interworking_select(dev[0], bssid, "home", freq="2412")

    dev[0].request("SET disallow_aps bssid " + bssid.translate(None, ':'))
    ret = dev[0].request("INTERWORKING_CONNECT " + bssid)
    if "FAIL" not in ret:
        raise Exception("INTERWORKING_CONNECT to disallowed BSS not rejected")

    if "FAIL" not in dev[0].request("INTERWORKING_CONNECT foo"):
        raise Exception("Invalid INTERWORKING_CONNECT not rejected")
    if "FAIL" not in dev[0].request("INTERWORKING_CONNECT 00:11:22:33:44:55"):
        raise Exception("Invalid INTERWORKING_CONNECT not rejected")

def policy_test(dev, ap, values, only_one=True):
    dev.dump_monitor()
    if ap:
        logger.info("Verify network selection to AP " + ap['ifname'])
        bssid = ap['bssid']
        dev.scan_for_bss(bssid, freq="2412")
    else:
        logger.info("Verify network selection")
        bssid = None
    dev.hs20_enable()
    id = dev.add_cred_values(values)
    dev.request("INTERWORKING_SELECT auto freq=2412")
    events = []
    while True:
        ev = dev.wait_event(["INTERWORKING-AP", "INTERWORKING-NO-MATCH",
                             "INTERWORKING-BLACKLISTED",
                             "INTERWORKING-SELECTED"], timeout=15)
        if ev is None:
            raise Exception("Network selection timed out")
        events.append(ev)
        if "INTERWORKING-NO-MATCH" in ev:
            raise Exception("Matching AP not found")
        if bssid and only_one and "INTERWORKING-AP" in ev and bssid not in ev:
            raise Exception("Unexpected AP claimed acceptable")
        if "INTERWORKING-SELECTED" in ev:
            if bssid and bssid not in ev:
                raise Exception("Selected incorrect BSS")
            break

    ev = dev.wait_connected(timeout=15)
    if bssid and bssid not in ev:
        raise Exception("Connected to incorrect BSS")

    conn_bssid = dev.get_status_field("bssid")
    if bssid and conn_bssid != bssid:
        raise Exception("bssid information points to incorrect BSS")

    dev.remove_cred(id)
    dev.dump_monitor()
    return events

def default_cred(domain=None):
    cred = { 'realm': "example.com",
             'ca_cert': "auth_serv/ca.pem",
             'username': "hs20-test",
             'password': "password" }
    if domain:
        cred['domain'] = domain
    return cred

def test_ap_hs20_prefer_home(dev, apdev):
    """Hotspot 2.0 required roaming consortium"""
    params = hs20_ap_params()
    params['domain_name'] = "example.org"
    hostapd.add_ap(apdev[0]['ifname'], params)

    params = hs20_ap_params()
    params['ssid'] = "test-hs20-other"
    params['domain_name'] = "example.com"
    hostapd.add_ap(apdev[1]['ifname'], params)

    values = default_cred()
    values['domain'] = "example.com"
    policy_test(dev[0], apdev[1], values, only_one=False)
    values['domain'] = "example.org"
    policy_test(dev[0], apdev[0], values, only_one=False)

def test_ap_hs20_req_roaming_consortium(dev, apdev):
    """Hotspot 2.0 required roaming consortium"""
    params = hs20_ap_params()
    hostapd.add_ap(apdev[0]['ifname'], params)

    params = hs20_ap_params()
    params['ssid'] = "test-hs20-other"
    params['roaming_consortium'] = [ "223344" ]
    hostapd.add_ap(apdev[1]['ifname'], params)

    values = default_cred()
    values['required_roaming_consortium'] = "223344"
    policy_test(dev[0], apdev[1], values)
    values['required_roaming_consortium'] = "112233"
    policy_test(dev[0], apdev[0], values)

    id = dev[0].add_cred()
    dev[0].set_cred(id, "required_roaming_consortium", "112233")
    dev[0].set_cred(id, "required_roaming_consortium", "112233445566778899aabbccddeeff")

    for val in [ "", "1", "11", "1122", "1122334", "112233445566778899aabbccddeeff00" ]:
        if "FAIL" not in dev[0].request('SET_CRED {} required_roaming_consortium {}'.format(id, val)):
            raise Exception("Invalid roaming consortium value accepted: " + val)

def test_ap_hs20_excluded_ssid(dev, apdev):
    """Hotspot 2.0 exclusion based on SSID"""
    params = hs20_ap_params()
    params['roaming_consortium'] = [ "223344" ]
    params['anqp_3gpp_cell_net'] = "555,444"
    hostapd.add_ap(apdev[0]['ifname'], params)

    params = hs20_ap_params()
    params['ssid'] = "test-hs20-other"
    params['roaming_consortium'] = [ "223344" ]
    params['anqp_3gpp_cell_net'] = "555,444"
    hostapd.add_ap(apdev[1]['ifname'], params)

    values = default_cred()
    values['excluded_ssid'] = "test-hs20"
    events = policy_test(dev[0], apdev[1], values)
    ev = [e for e in events if "INTERWORKING-BLACKLISTED " + apdev[0]['bssid'] in e]
    if len(ev) != 1:
        raise Exception("Excluded network not reported")
    values['excluded_ssid'] = "test-hs20-other"
    events = policy_test(dev[0], apdev[0], values)
    ev = [e for e in events if "INTERWORKING-BLACKLISTED " + apdev[1]['bssid'] in e]
    if len(ev) != 1:
        raise Exception("Excluded network not reported")

    values = default_cred()
    values['roaming_consortium'] = "223344"
    values['eap'] = "TTLS"
    values['phase2'] = "auth=MSCHAPV2"
    values['excluded_ssid'] = "test-hs20"
    events = policy_test(dev[0], apdev[1], values)
    ev = [e for e in events if "INTERWORKING-BLACKLISTED " + apdev[0]['bssid'] in e]
    if len(ev) != 1:
        raise Exception("Excluded network not reported")

    values = { 'imsi': "555444-333222111", 'eap': "SIM",
               'milenage': "5122250214c33e723a5dd523fc145fc0:981d464c7c52eb6e5036234984ad0bcf:000000000123",
               'excluded_ssid': "test-hs20" }
    events = policy_test(dev[0], apdev[1], values)
    ev = [e for e in events if "INTERWORKING-BLACKLISTED " + apdev[0]['bssid'] in e]
    if len(ev) != 1:
        raise Exception("Excluded network not reported")

def test_ap_hs20_roam_to_higher_prio(dev, apdev):
    """Hotspot 2.0 and roaming from current to higher priority network"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params(ssid="test-hs20-visited")
    params['domain_name'] = "visited.example.org"
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    id = dev[0].add_cred_values({ 'realm': "example.com",
                                  'ca_cert': "auth_serv/ca.pem",
                                  'username': "hs20-test",
                                  'password': "password",
                                  'domain': "example.com" })
    logger.info("Connect to the only network option")
    interworking_select(dev[0], bssid, "roaming", freq="2412")
    dev[0].dump_monitor()
    interworking_connect(dev[0], bssid, "TTLS")

    logger.info("Start another AP (home operator) and reconnect")
    bssid2 = apdev[1]['bssid']
    params = hs20_ap_params(ssid="test-hs20-home")
    params['domain_name'] = "example.com"
    hostapd.add_ap(apdev[1]['ifname'], params)

    dev[0].scan_for_bss(bssid2, freq="2412", force_scan=True)
    dev[0].request("INTERWORKING_SELECT auto freq=2412")
    ev = dev[0].wait_event(["INTERWORKING-NO-MATCH",
                            "INTERWORKING-ALREADY-CONNECTED",
                            "CTRL-EVENT-CONNECTED"], timeout=15)
    if ev is None:
        raise Exception("Connection timed out")
    if "INTERWORKING-NO-MATCH" in ev:
        raise Exception("Matching AP not found")
    if "INTERWORKING-ALREADY-CONNECTED" in ev:
        raise Exception("Unexpected AP selected")
    if bssid2 not in ev:
        raise Exception("Unexpected BSSID after reconnection")

def test_ap_hs20_domain_suffix_match_full(dev, apdev):
    """Hotspot 2.0 and domain_suffix_match"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    id = dev[0].add_cred_values({ 'realm': "example.com",
                                  'username': "hs20-test",
                                  'password': "password",
                                  'ca_cert': "auth_serv/ca.pem",
                                  'domain': "example.com",
                                  'domain_suffix_match': "server.w1.fi" })
    interworking_select(dev[0], bssid, "home", freq="2412")
    dev[0].dump_monitor()
    interworking_connect(dev[0], bssid, "TTLS")
    dev[0].request("REMOVE_NETWORK all")
    dev[0].dump_monitor()

    dev[0].set_cred_quoted(id, "domain_suffix_match", "no-match.example.com")
    interworking_select(dev[0], bssid, "home", freq="2412")
    dev[0].dump_monitor()
    dev[0].request("INTERWORKING_CONNECT " + bssid)
    ev = dev[0].wait_event(["CTRL-EVENT-EAP-TLS-CERT-ERROR"])
    if ev is None:
        raise Exception("TLS certificate error not reported")
    if "Domain suffix mismatch" not in ev:
        raise Exception("Domain suffix mismatch not reported")

def test_ap_hs20_domain_suffix_match(dev, apdev):
    """Hotspot 2.0 and domain_suffix_match"""
    check_domain_match_full(dev[0])
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    id = dev[0].add_cred_values({ 'realm': "example.com",
                                  'username': "hs20-test",
                                  'password': "password",
                                  'ca_cert': "auth_serv/ca.pem",
                                  'domain': "example.com",
                                  'domain_suffix_match': "w1.fi" })
    interworking_select(dev[0], bssid, "home", freq="2412")
    dev[0].dump_monitor()
    interworking_connect(dev[0], bssid, "TTLS")

def test_ap_hs20_roaming_partner_preference(dev, apdev):
    """Hotspot 2.0 and roaming partner preference"""
    params = hs20_ap_params()
    params['domain_name'] = "roaming.example.org"
    hostapd.add_ap(apdev[0]['ifname'], params)

    params = hs20_ap_params()
    params['ssid'] = "test-hs20-other"
    params['domain_name'] = "roaming.example.net"
    hostapd.add_ap(apdev[1]['ifname'], params)

    logger.info("Verify default vs. specified preference")
    values = default_cred()
    values['roaming_partner'] = "roaming.example.net,1,127,*"
    policy_test(dev[0], apdev[1], values, only_one=False)
    values['roaming_partner'] = "roaming.example.net,1,129,*"
    policy_test(dev[0], apdev[0], values, only_one=False)

    logger.info("Verify partial FQDN match")
    values['roaming_partner'] = "example.net,0,0,*"
    policy_test(dev[0], apdev[1], values, only_one=False)
    values['roaming_partner'] = "example.net,0,255,*"
    policy_test(dev[0], apdev[0], values, only_one=False)

def test_ap_hs20_max_bss_load(dev, apdev):
    """Hotspot 2.0 and maximum BSS load"""
    params = hs20_ap_params()
    params['bss_load_test'] = "12:200:20000"
    hostapd.add_ap(apdev[0]['ifname'], params)

    params = hs20_ap_params()
    params['ssid'] = "test-hs20-other"
    params['bss_load_test'] = "5:20:10000"
    hostapd.add_ap(apdev[1]['ifname'], params)

    logger.info("Verify maximum BSS load constraint")
    values = default_cred()
    values['domain'] = "example.com"
    values['max_bss_load'] = "100"
    events = policy_test(dev[0], apdev[1], values, only_one=False)

    ev = [e for e in events if "INTERWORKING-AP " + apdev[0]['bssid'] in e]
    if len(ev) != 1 or "over_max_bss_load=1" not in ev[0]:
        raise Exception("Maximum BSS Load case not noticed")
    ev = [e for e in events if "INTERWORKING-AP " + apdev[1]['bssid'] in e]
    if len(ev) != 1 or "over_max_bss_load=1" in ev[0]:
        raise Exception("Maximum BSS Load case reported incorrectly")

    logger.info("Verify maximum BSS load does not prevent connection")
    values['max_bss_load'] = "1"
    events = policy_test(dev[0], None, values)

    ev = [e for e in events if "INTERWORKING-AP " + apdev[0]['bssid'] in e]
    if len(ev) != 1 or "over_max_bss_load=1" not in ev[0]:
        raise Exception("Maximum BSS Load case not noticed")
    ev = [e for e in events if "INTERWORKING-AP " + apdev[1]['bssid'] in e]
    if len(ev) != 1 or "over_max_bss_load=1" not in ev[0]:
        raise Exception("Maximum BSS Load case not noticed")

def test_ap_hs20_max_bss_load2(dev, apdev):
    """Hotspot 2.0 and maximum BSS load with one AP not advertising"""
    params = hs20_ap_params()
    params['bss_load_test'] = "12:200:20000"
    hostapd.add_ap(apdev[0]['ifname'], params)

    params = hs20_ap_params()
    params['ssid'] = "test-hs20-other"
    hostapd.add_ap(apdev[1]['ifname'], params)

    logger.info("Verify maximum BSS load constraint with AP advertisement")
    values = default_cred()
    values['domain'] = "example.com"
    values['max_bss_load'] = "100"
    events = policy_test(dev[0], apdev[1], values, only_one=False)

    ev = [e for e in events if "INTERWORKING-AP " + apdev[0]['bssid'] in e]
    if len(ev) != 1 or "over_max_bss_load=1" not in ev[0]:
        raise Exception("Maximum BSS Load case not noticed")
    ev = [e for e in events if "INTERWORKING-AP " + apdev[1]['bssid'] in e]
    if len(ev) != 1 or "over_max_bss_load=1" in ev[0]:
        raise Exception("Maximum BSS Load case reported incorrectly")

def test_ap_hs20_multi_cred_sp_prio(dev, apdev):
    """Hotspot 2.0 multi-cred sp_priority"""
    try:
        _test_ap_hs20_multi_cred_sp_prio(dev, apdev)
    finally:
        dev[0].request("SET external_sim 0")

def _test_ap_hs20_multi_cred_sp_prio(dev, apdev):
    hlr_auc_gw_available()
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['hessid'] = bssid
    del params['domain_name']
    params['anqp_3gpp_cell_net'] = "232,01"
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    dev[0].scan_for_bss(bssid, freq="2412")
    dev[0].request("SET external_sim 1")
    id1 = dev[0].add_cred_values({ 'imsi': "23201-0000000000", 'eap': "SIM",
                                   'provisioning_sp': "example.com",
                                   'sp_priority' :"1" })
    id2 = dev[0].add_cred_values({ 'realm': "example.com",
                                   'ca_cert': "auth_serv/ca.pem",
                                   'username': "hs20-test",
                                   'password': "password",
                                   'domain': "example.com",
                                   'provisioning_sp': "example.com",
                                   'sp_priority': "2" })
    dev[0].dump_monitor()
    dev[0].scan_for_bss(bssid, freq="2412")
    dev[0].request("INTERWORKING_SELECT auto freq=2412")
    interworking_ext_sim_auth(dev[0], "SIM")
    check_sp_type(dev[0], "unknown")
    dev[0].request("REMOVE_NETWORK all")

    dev[0].set_cred(id1, "sp_priority", "2")
    dev[0].set_cred(id2, "sp_priority", "1")
    dev[0].dump_monitor()
    dev[0].request("INTERWORKING_SELECT auto freq=2412")
    interworking_auth(dev[0], "TTLS")
    check_sp_type(dev[0], "unknown")

def test_ap_hs20_multi_cred_sp_prio2(dev, apdev):
    """Hotspot 2.0 multi-cred sp_priority with two BSSes"""
    try:
        _test_ap_hs20_multi_cred_sp_prio2(dev, apdev)
    finally:
        dev[0].request("SET external_sim 0")

def _test_ap_hs20_multi_cred_sp_prio2(dev, apdev):
    hlr_auc_gw_available()
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['hessid'] = bssid
    del params['nai_realm']
    del params['domain_name']
    params['anqp_3gpp_cell_net'] = "232,01"
    hostapd.add_ap(apdev[0]['ifname'], params)

    bssid2 = apdev[1]['bssid']
    params = hs20_ap_params()
    params['ssid'] = "test-hs20-other"
    params['hessid'] = bssid2
    del params['domain_name']
    del params['anqp_3gpp_cell_net']
    hostapd.add_ap(apdev[1]['ifname'], params)

    dev[0].hs20_enable()
    dev[0].request("SET external_sim 1")
    id1 = dev[0].add_cred_values({ 'imsi': "23201-0000000000", 'eap': "SIM",
                                   'provisioning_sp': "example.com",
                                   'sp_priority': "1" })
    id2 = dev[0].add_cred_values({ 'realm': "example.com",
                                   'ca_cert': "auth_serv/ca.pem",
                                   'username': "hs20-test",
                                   'password': "password",
                                   'domain': "example.com",
                                   'provisioning_sp': "example.com",
                                   'sp_priority': "2" })
    dev[0].dump_monitor()
    dev[0].scan_for_bss(bssid, freq="2412")
    dev[0].scan_for_bss(bssid2, freq="2412")
    dev[0].request("INTERWORKING_SELECT auto freq=2412")
    interworking_ext_sim_auth(dev[0], "SIM")
    check_sp_type(dev[0], "unknown")
    conn_bssid = dev[0].get_status_field("bssid")
    if conn_bssid != bssid:
        raise Exception("Connected to incorrect BSS")
    dev[0].request("REMOVE_NETWORK all")

    dev[0].set_cred(id1, "sp_priority", "2")
    dev[0].set_cred(id2, "sp_priority", "1")
    dev[0].dump_monitor()
    dev[0].request("INTERWORKING_SELECT auto freq=2412")
    interworking_auth(dev[0], "TTLS")
    check_sp_type(dev[0], "unknown")
    conn_bssid = dev[0].get_status_field("bssid")
    if conn_bssid != bssid2:
        raise Exception("Connected to incorrect BSS")

def check_conn_capab_selection(dev, type, missing):
    dev.request("INTERWORKING_SELECT freq=2412")
    ev = dev.wait_event(["INTERWORKING-AP"])
    if ev is None:
        raise Exception("Network selection timed out");
    if "type=" + type not in ev:
        raise Exception("Unexpected network type")
    if missing and "conn_capab_missing=1" not in ev:
        raise Exception("conn_capab_missing not reported")
    if not missing and "conn_capab_missing=1" in ev:
        raise Exception("conn_capab_missing reported unexpectedly")

def conn_capab_cred(domain=None, req_conn_capab=None):
    cred = default_cred(domain=domain)
    if req_conn_capab:
        cred['req_conn_capab'] = req_conn_capab
    return cred

def test_ap_hs20_req_conn_capab(dev, apdev):
    """Hotspot 2.0 network selection with req_conn_capab"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    dev[0].scan_for_bss(bssid, freq="2412")
    logger.info("Not used in home network")
    values = conn_capab_cred(domain="example.com", req_conn_capab="6:1234")
    id = dev[0].add_cred_values(values)
    check_conn_capab_selection(dev[0], "home", False)

    logger.info("Used in roaming network")
    dev[0].remove_cred(id)
    values = conn_capab_cred(domain="example.org", req_conn_capab="6:1234")
    id = dev[0].add_cred_values(values)
    check_conn_capab_selection(dev[0], "roaming", True)

    logger.info("Verify that req_conn_capab does not prevent connection if no other network is available")
    check_auto_select(dev[0], bssid)

    logger.info("Additional req_conn_capab checks")

    dev[0].remove_cred(id)
    values = conn_capab_cred(domain="example.org", req_conn_capab="1:0")
    id = dev[0].add_cred_values(values)
    check_conn_capab_selection(dev[0], "roaming", True)

    dev[0].remove_cred(id)
    values = conn_capab_cred(domain="example.org", req_conn_capab="17:5060")
    id = dev[0].add_cred_values(values)
    check_conn_capab_selection(dev[0], "roaming", True)

    bssid2 = apdev[1]['bssid']
    params = hs20_ap_params(ssid="test-hs20b")
    params['hs20_conn_capab'] = [ "1:0:2", "6:22:1", "17:5060:0", "50:0:1" ]
    hostapd.add_ap(apdev[1]['ifname'], params)

    dev[0].remove_cred(id)
    values = conn_capab_cred(domain="example.org", req_conn_capab="50")
    id = dev[0].add_cred_values(values)
    dev[0].set_cred(id, "req_conn_capab", "6:22")
    dev[0].scan_for_bss(bssid2, freq="2412")
    dev[0].request("INTERWORKING_SELECT freq=2412")
    for i in range(0, 2):
        ev = dev[0].wait_event(["INTERWORKING-AP"])
        if ev is None:
            raise Exception("Network selection timed out");
        if bssid in ev and "conn_capab_missing=1" not in ev:
            raise Exception("Missing protocol connection capability not reported")
        if bssid2 in ev and "conn_capab_missing=1" in ev:
            raise Exception("Protocol connection capability not reported correctly")

def test_ap_hs20_req_conn_capab_and_roaming_partner_preference(dev, apdev):
    """Hotspot 2.0 and req_conn_capab with roaming partner preference"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['domain_name'] = "roaming.example.org"
    params['hs20_conn_capab'] = [ "1:0:2", "6:22:1", "17:5060:0", "50:0:1" ]
    hostapd.add_ap(apdev[0]['ifname'], params)

    bssid2 = apdev[1]['bssid']
    params = hs20_ap_params(ssid="test-hs20-b")
    params['domain_name'] = "roaming.example.net"
    hostapd.add_ap(apdev[1]['ifname'], params)

    values = default_cred()
    values['roaming_partner'] = "roaming.example.net,1,127,*"
    id = dev[0].add_cred_values(values)
    check_auto_select(dev[0], bssid2)

    dev[0].set_cred(id, "req_conn_capab", "50")
    check_auto_select(dev[0], bssid)

    dev[0].remove_cred(id)
    id = dev[0].add_cred_values(values)
    dev[0].set_cred(id, "req_conn_capab", "51")
    check_auto_select(dev[0], bssid2)

def check_bandwidth_selection(dev, type, below):
    dev.request("INTERWORKING_SELECT freq=2412")
    ev = dev.wait_event(["INTERWORKING-AP"])
    if ev is None:
        raise Exception("Network selection timed out");
    logger.debug("BSS entries:\n" + dev.request("BSS RANGE=ALL"))
    if "type=" + type not in ev:
        raise Exception("Unexpected network type")
    if below and "below_min_backhaul=1" not in ev:
        raise Exception("below_min_backhaul not reported")
    if not below and "below_min_backhaul=1" in ev:
        raise Exception("below_min_backhaul reported unexpectedly")

def bw_cred(domain=None, dl_home=None, ul_home=None, dl_roaming=None, ul_roaming=None):
    cred = default_cred(domain=domain)
    if dl_home:
        cred['min_dl_bandwidth_home'] = str(dl_home)
    if ul_home:
        cred['min_ul_bandwidth_home'] = str(ul_home)
    if dl_roaming:
        cred['min_dl_bandwidth_roaming'] = str(dl_roaming)
    if ul_roaming:
        cred['min_ul_bandwidth_roaming'] = str(ul_roaming)
    return cred

def test_ap_hs20_min_bandwidth_home(dev, apdev):
    """Hotspot 2.0 network selection with min bandwidth (home)"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    dev[0].scan_for_bss(bssid, freq="2412")
    values = bw_cred(domain="example.com", dl_home=5490, ul_home=58)
    id = dev[0].add_cred_values(values)
    check_bandwidth_selection(dev[0], "home", False)
    dev[0].remove_cred(id)

    values = bw_cred(domain="example.com", dl_home=5491, ul_home=58)
    id = dev[0].add_cred_values(values)
    check_bandwidth_selection(dev[0], "home", True)
    dev[0].remove_cred(id)

    values = bw_cred(domain="example.com", dl_home=5490, ul_home=59)
    id = dev[0].add_cred_values(values)
    check_bandwidth_selection(dev[0], "home", True)
    dev[0].remove_cred(id)

    values = bw_cred(domain="example.com", dl_home=5491, ul_home=59)
    id = dev[0].add_cred_values(values)
    check_bandwidth_selection(dev[0], "home", True)
    check_auto_select(dev[0], bssid)

    bssid2 = apdev[1]['bssid']
    params = hs20_ap_params(ssid="test-hs20-b")
    params['hs20_wan_metrics'] = "01:8000:1000:1:1:3000"
    hostapd.add_ap(apdev[1]['ifname'], params)

    check_auto_select(dev[0], bssid2)

def test_ap_hs20_min_bandwidth_home_hidden_ssid_in_scan_res(dev, apdev):
    """Hotspot 2.0 network selection with min bandwidth (home) while hidden SSID is included in scan results"""
    bssid = apdev[0]['bssid']

    hapd = hostapd.add_ap(apdev[0]['ifname'], { "ssid": 'secret',
                                                "ignore_broadcast_ssid": "1" })
    dev[0].scan_for_bss(bssid, freq=2412)
    hapd.disable()
    hapd_global = hostapd.HostapdGlobal()
    hapd_global.flush()
    hapd_global.remove(apdev[0]['ifname'])

    params = hs20_ap_params()
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    dev[0].scan_for_bss(bssid, freq="2412")
    values = bw_cred(domain="example.com", dl_home=5490, ul_home=58)
    id = dev[0].add_cred_values(values)
    check_bandwidth_selection(dev[0], "home", False)
    dev[0].remove_cred(id)

    values = bw_cred(domain="example.com", dl_home=5491, ul_home=58)
    id = dev[0].add_cred_values(values)
    check_bandwidth_selection(dev[0], "home", True)
    dev[0].remove_cred(id)

    values = bw_cred(domain="example.com", dl_home=5490, ul_home=59)
    id = dev[0].add_cred_values(values)
    check_bandwidth_selection(dev[0], "home", True)
    dev[0].remove_cred(id)

    values = bw_cred(domain="example.com", dl_home=5491, ul_home=59)
    id = dev[0].add_cred_values(values)
    check_bandwidth_selection(dev[0], "home", True)
    check_auto_select(dev[0], bssid)

    bssid2 = apdev[1]['bssid']
    params = hs20_ap_params(ssid="test-hs20-b")
    params['hs20_wan_metrics'] = "01:8000:1000:1:1:3000"
    hostapd.add_ap(apdev[1]['ifname'], params)

    check_auto_select(dev[0], bssid2)

    dev[0].flush_scan_cache()

def test_ap_hs20_min_bandwidth_roaming(dev, apdev):
    """Hotspot 2.0 network selection with min bandwidth (roaming)"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    dev[0].scan_for_bss(bssid, freq="2412")
    values = bw_cred(domain="example.org", dl_roaming=5490, ul_roaming=58)
    id = dev[0].add_cred_values(values)
    check_bandwidth_selection(dev[0], "roaming", False)
    dev[0].remove_cred(id)

    values = bw_cred(domain="example.org", dl_roaming=5491, ul_roaming=58)
    id = dev[0].add_cred_values(values)
    check_bandwidth_selection(dev[0], "roaming", True)
    dev[0].remove_cred(id)

    values = bw_cred(domain="example.org", dl_roaming=5490, ul_roaming=59)
    id = dev[0].add_cred_values(values)
    check_bandwidth_selection(dev[0], "roaming", True)
    dev[0].remove_cred(id)

    values = bw_cred(domain="example.org", dl_roaming=5491, ul_roaming=59)
    id = dev[0].add_cred_values(values)
    check_bandwidth_selection(dev[0], "roaming", True)
    check_auto_select(dev[0], bssid)

    bssid2 = apdev[1]['bssid']
    params = hs20_ap_params(ssid="test-hs20-b")
    params['hs20_wan_metrics'] = "01:8000:1000:1:1:3000"
    hostapd.add_ap(apdev[1]['ifname'], params)

    check_auto_select(dev[0], bssid2)

def test_ap_hs20_min_bandwidth_and_roaming_partner_preference(dev, apdev):
    """Hotspot 2.0 and minimum bandwidth with roaming partner preference"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['domain_name'] = "roaming.example.org"
    params['hs20_wan_metrics'] = "01:8000:1000:1:1:3000"
    hostapd.add_ap(apdev[0]['ifname'], params)

    bssid2 = apdev[1]['bssid']
    params = hs20_ap_params(ssid="test-hs20-b")
    params['domain_name'] = "roaming.example.net"
    hostapd.add_ap(apdev[1]['ifname'], params)

    values = default_cred()
    values['roaming_partner'] = "roaming.example.net,1,127,*"
    id = dev[0].add_cred_values(values)
    check_auto_select(dev[0], bssid2)

    dev[0].set_cred(id, "min_dl_bandwidth_roaming", "6000")
    check_auto_select(dev[0], bssid)

    dev[0].set_cred(id, "min_dl_bandwidth_roaming", "10000")
    check_auto_select(dev[0], bssid2)

def test_ap_hs20_min_bandwidth_no_wan_metrics(dev, apdev):
    """Hotspot 2.0 network selection with min bandwidth but no WAN Metrics"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    del params['hs20_wan_metrics']
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    dev[0].scan_for_bss(bssid, freq="2412")
    values = bw_cred(domain="example.com", dl_home=10000, ul_home=10000,
                     dl_roaming=10000, ul_roaming=10000)
    dev[0].add_cred_values(values)
    check_bandwidth_selection(dev[0], "home", False)

def test_ap_hs20_deauth_req_ess(dev, apdev):
    """Hotspot 2.0 connection and deauthentication request for ESS"""
    try:
        _test_ap_hs20_deauth_req_ess(dev, apdev)
    finally:
        dev[0].request("SET pmf 0")

def _test_ap_hs20_deauth_req_ess(dev, apdev):
    dev[0].request("SET pmf 2")
    eap_test(dev[0], apdev[0], "21[3:26]", "TTLS", "user")
    dev[0].dump_monitor()
    addr = dev[0].p2p_interface_addr()
    hapd = hostapd.Hostapd(apdev[0]['ifname'])
    hapd.request("HS20_DEAUTH_REQ " + addr + " 1 120 http://example.com/")
    ev = dev[0].wait_event(["HS20-DEAUTH-IMMINENT-NOTICE"])
    if ev is None:
        raise Exception("Timeout on deauth imminent notice")
    if "1 120 http://example.com/" not in ev:
        raise Exception("Unexpected deauth imminent notice: " + ev)
    hapd.request("DEAUTHENTICATE " + addr)
    dev[0].wait_disconnected(timeout=10)
    if "[TEMP-DISABLED]" not in dev[0].list_networks()[0]['flags']:
        raise Exception("Network not marked temporarily disabled")
    ev = dev[0].wait_event(["SME: Trying to authenticate",
                            "Trying to associate",
                            "CTRL-EVENT-CONNECTED"], timeout=5)
    if ev is not None:
        raise Exception("Unexpected connection attempt")

def test_ap_hs20_deauth_req_bss(dev, apdev):
    """Hotspot 2.0 connection and deauthentication request for BSS"""
    try:
        _test_ap_hs20_deauth_req_bss(dev, apdev)
    finally:
        dev[0].request("SET pmf 0")

def _test_ap_hs20_deauth_req_bss(dev, apdev):
    dev[0].request("SET pmf 2")
    eap_test(dev[0], apdev[0], "21[3:26]", "TTLS", "user")
    dev[0].dump_monitor()
    addr = dev[0].p2p_interface_addr()
    hapd = hostapd.Hostapd(apdev[0]['ifname'])
    hapd.request("HS20_DEAUTH_REQ " + addr + " 0 120 http://example.com/")
    ev = dev[0].wait_event(["HS20-DEAUTH-IMMINENT-NOTICE"])
    if ev is None:
        raise Exception("Timeout on deauth imminent notice")
    if "0 120 http://example.com/" not in ev:
        raise Exception("Unexpected deauth imminent notice: " + ev)
    hapd.request("DEAUTHENTICATE " + addr + " reason=4")
    ev = dev[0].wait_disconnected(timeout=10)
    if "reason=4" not in ev:
        raise Exception("Unexpected disconnection reason")
    if "[TEMP-DISABLED]" not in dev[0].list_networks()[0]['flags']:
        raise Exception("Network not marked temporarily disabled")
    ev = dev[0].wait_event(["SME: Trying to authenticate",
                            "Trying to associate",
                            "CTRL-EVENT-CONNECTED"], timeout=5)
    if ev is not None:
        raise Exception("Unexpected connection attempt")

def test_ap_hs20_deauth_req_from_radius(dev, apdev):
    """Hotspot 2.0 connection and deauthentication request from RADIUS"""
    try:
        _test_ap_hs20_deauth_req_from_radius(dev, apdev)
    finally:
        dev[0].request("SET pmf 0")

def _test_ap_hs20_deauth_req_from_radius(dev, apdev):
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['nai_realm'] = [ "0,example.com,21[2:4]" ]
    params['hs20_deauth_req_timeout'] = "2"
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].request("SET pmf 2")
    dev[0].hs20_enable()
    dev[0].add_cred_values({ 'realm': "example.com",
                             'username': "hs20-deauth-test",
                             'password': "password" })
    interworking_select(dev[0], bssid, freq="2412")
    interworking_connect(dev[0], bssid, "TTLS")
    ev = dev[0].wait_event(["HS20-DEAUTH-IMMINENT-NOTICE"], timeout=5)
    if ev is None:
        raise Exception("Timeout on deauth imminent notice")
    if " 1 100" not in ev:
        raise Exception("Unexpected deauth imminent contents")
    dev[0].wait_disconnected(timeout=3)

def test_ap_hs20_remediation_required(dev, apdev):
    """Hotspot 2.0 connection and remediation required from RADIUS"""
    try:
        _test_ap_hs20_remediation_required(dev, apdev)
    finally:
        dev[0].request("SET pmf 0")

def _test_ap_hs20_remediation_required(dev, apdev):
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['nai_realm'] = [ "0,example.com,21[2:4]" ]
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].request("SET pmf 1")
    dev[0].hs20_enable()
    dev[0].add_cred_values({ 'realm': "example.com",
                             'username': "hs20-subrem-test",
                             'password': "password" })
    interworking_select(dev[0], bssid, freq="2412")
    interworking_connect(dev[0], bssid, "TTLS")
    ev = dev[0].wait_event(["HS20-SUBSCRIPTION-REMEDIATION"], timeout=5)
    if ev is None:
        raise Exception("Timeout on subscription remediation notice")
    if " 1 https://example.com/" not in ev:
        raise Exception("Unexpected subscription remediation event contents")

def test_ap_hs20_remediation_required_ctrl(dev, apdev):
    """Hotspot 2.0 connection and subrem from ctrl_iface"""
    try:
        _test_ap_hs20_remediation_required_ctrl(dev, apdev)
    finally:
        dev[0].request("SET pmf 0")

def _test_ap_hs20_remediation_required_ctrl(dev, apdev):
    bssid = apdev[0]['bssid']
    addr = dev[0].p2p_dev_addr()
    params = hs20_ap_params()
    params['nai_realm'] = [ "0,example.com,21[2:4]" ]
    hapd = hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].request("SET pmf 1")
    dev[0].hs20_enable()
    dev[0].add_cred_values(default_cred())
    interworking_select(dev[0], bssid, freq="2412")
    interworking_connect(dev[0], bssid, "TTLS")

    hapd.request("HS20_WNM_NOTIF " + addr + " https://example.com/")
    ev = dev[0].wait_event(["HS20-SUBSCRIPTION-REMEDIATION"], timeout=5)
    if ev is None:
        raise Exception("Timeout on subscription remediation notice")
    if " 1 https://example.com/" not in ev:
        raise Exception("Unexpected subscription remediation event contents")

    hapd.request("HS20_WNM_NOTIF " + addr)
    ev = dev[0].wait_event(["HS20-SUBSCRIPTION-REMEDIATION"], timeout=5)
    if ev is None:
        raise Exception("Timeout on subscription remediation notice")
    if not ev.endswith("HS20-SUBSCRIPTION-REMEDIATION "):
        raise Exception("Unexpected subscription remediation event contents: " + ev)

    if "FAIL" not in hapd.request("HS20_WNM_NOTIF "):
        raise Exception("Unexpected HS20_WNM_NOTIF success")
    if "FAIL" not in hapd.request("HS20_WNM_NOTIF foo"):
        raise Exception("Unexpected HS20_WNM_NOTIF success")
    if "FAIL" not in hapd.request("HS20_WNM_NOTIF " + addr + " https://12345678923456789842345678456783456712345678923456789842345678456783456712345678923456789842345678456783456712345678923456789842345678456783456712345678923456789842345678456783456712345678923456789842345678456783456712345678923456789842345678456783456712345678927.very.long.example.com/"):
        raise Exception("Unexpected HS20_WNM_NOTIF success")

def test_ap_hs20_session_info(dev, apdev):
    """Hotspot 2.0 connection and session information from RADIUS"""
    try:
        _test_ap_hs20_session_info(dev, apdev)
    finally:
        dev[0].request("SET pmf 0")

def _test_ap_hs20_session_info(dev, apdev):
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['nai_realm'] = [ "0,example.com,21[2:4]" ]
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].request("SET pmf 1")
    dev[0].hs20_enable()
    dev[0].add_cred_values({ 'realm': "example.com",
                             'username': "hs20-session-info-test",
                             'password': "password" })
    interworking_select(dev[0], bssid, freq="2412")
    interworking_connect(dev[0], bssid, "TTLS")
    ev = dev[0].wait_event(["ESS-DISASSOC-IMMINENT"], timeout=10)
    if ev is None:
        raise Exception("Timeout on ESS disassociation imminent notice")
    if " 1 59904 https://example.com/" not in ev:
        raise Exception("Unexpected ESS disassociation imminent event contents")
    ev = dev[0].wait_event(["CTRL-EVENT-SCAN-STARTED"])
    if ev is None:
        raise Exception("Scan not started")
    ev = dev[0].wait_event(["CTRL-EVENT-SCAN-RESULTS"], timeout=30)
    if ev is None:
        raise Exception("Scan not completed")

def test_ap_hs20_osen(dev, apdev):
    """Hotspot 2.0 OSEN connection"""
    params = { 'ssid': "osen",
               'osen': "1",
               'auth_server_addr': "127.0.0.1",
               'auth_server_port': "1812",
               'auth_server_shared_secret': "radius" }
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[1].connect("osen", key_mgmt="NONE", scan_freq="2412",
                   wait_connect=False)
    dev[2].connect("osen", key_mgmt="NONE", wep_key0='"hello"',
                   scan_freq="2412", wait_connect=False)
    dev[0].connect("osen", proto="OSEN", key_mgmt="OSEN", pairwise="CCMP",
                   group="GTK_NOT_USED",
                   eap="WFA-UNAUTH-TLS", identity="osen@example.com",
                   ca_cert="auth_serv/ca.pem",
                   scan_freq="2412")

    wpas = WpaSupplicant(global_iface='/tmp/wpas-wlan5')
    wpas.interface_add("wlan5", drv_params="force_connect_cmd=1")
    wpas.connect("osen", proto="OSEN", key_mgmt="OSEN", pairwise="CCMP",
                 group="GTK_NOT_USED",
                 eap="WFA-UNAUTH-TLS", identity="osen@example.com",
                 ca_cert="auth_serv/ca.pem",
                 scan_freq="2412")
    wpas.request("DISCONNECT")

def test_ap_hs20_network_preference(dev, apdev):
    """Hotspot 2.0 network selection with preferred home network"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    values = { 'realm': "example.com",
               'username': "hs20-test",
               'password': "password",
               'domain': "example.com" }
    dev[0].add_cred_values(values)

    id = dev[0].add_network()
    dev[0].set_network_quoted(id, "ssid", "home")
    dev[0].set_network_quoted(id, "psk", "12345678")
    dev[0].set_network(id, "priority", "1")
    dev[0].request("ENABLE_NETWORK %s no-connect" % id)

    dev[0].scan_for_bss(bssid, freq="2412")
    dev[0].request("INTERWORKING_SELECT auto freq=2412")
    ev = dev[0].wait_connected(timeout=15)
    if bssid not in ev:
        raise Exception("Unexpected network selected")

    bssid2 = apdev[1]['bssid']
    params = hostapd.wpa2_params(ssid="home", passphrase="12345678")
    hostapd.add_ap(apdev[1]['ifname'], params)

    dev[0].scan_for_bss(bssid2, freq="2412")
    dev[0].request("INTERWORKING_SELECT auto freq=2412")
    ev = dev[0].wait_event(["CTRL-EVENT-CONNECTED",
                            "INTERWORKING-ALREADY-CONNECTED" ], timeout=15)
    if ev is None:
        raise Exception("Connection timed out")
    if "INTERWORKING-ALREADY-CONNECTED" in ev:
        raise Exception("No roam to higher priority network")
    if bssid2 not in ev:
        raise Exception("Unexpected network selected")

def test_ap_hs20_network_preference2(dev, apdev):
    """Hotspot 2.0 network selection with preferred credential"""
    bssid2 = apdev[1]['bssid']
    params = hostapd.wpa2_params(ssid="home", passphrase="12345678")
    hostapd.add_ap(apdev[1]['ifname'], params)

    dev[0].hs20_enable()
    values = { 'realm': "example.com",
               'username': "hs20-test",
               'password': "password",
               'domain': "example.com",
               'priority': "1" }
    dev[0].add_cred_values(values)

    id = dev[0].add_network()
    dev[0].set_network_quoted(id, "ssid", "home")
    dev[0].set_network_quoted(id, "psk", "12345678")
    dev[0].request("ENABLE_NETWORK %s no-connect" % id)

    dev[0].scan_for_bss(bssid2, freq="2412")
    dev[0].request("INTERWORKING_SELECT auto freq=2412")
    ev = dev[0].wait_connected(timeout=15)
    if bssid2 not in ev:
        raise Exception("Unexpected network selected")

    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].scan_for_bss(bssid, freq="2412")
    dev[0].request("INTERWORKING_SELECT auto freq=2412")
    ev = dev[0].wait_event(["CTRL-EVENT-CONNECTED",
                            "INTERWORKING-ALREADY-CONNECTED" ], timeout=15)
    if ev is None:
        raise Exception("Connection timed out")
    if "INTERWORKING-ALREADY-CONNECTED" in ev:
        raise Exception("No roam to higher priority network")
    if bssid not in ev:
        raise Exception("Unexpected network selected")

def test_ap_hs20_network_preference3(dev, apdev):
    """Hotspot 2.0 network selection with two credential (one preferred)"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    hostapd.add_ap(apdev[0]['ifname'], params)

    bssid2 = apdev[1]['bssid']
    params = hs20_ap_params(ssid="test-hs20b")
    params['nai_realm'] = "0,example.org,13[5:6],21[2:4][5:7]"
    hostapd.add_ap(apdev[1]['ifname'], params)

    dev[0].hs20_enable()
    values = { 'realm': "example.com",
               'username': "hs20-test",
               'password': "password",
               'priority': "1" }
    dev[0].add_cred_values(values)
    values = { 'realm': "example.org",
               'username': "hs20-test",
               'password': "password" }
    id = dev[0].add_cred_values(values)

    dev[0].scan_for_bss(bssid, freq="2412")
    dev[0].scan_for_bss(bssid2, freq="2412")
    dev[0].request("INTERWORKING_SELECT auto freq=2412")
    ev = dev[0].wait_connected(timeout=15)
    if bssid not in ev:
        raise Exception("Unexpected network selected")

    dev[0].set_cred(id, "priority", "2")
    dev[0].request("INTERWORKING_SELECT auto freq=2412")
    ev = dev[0].wait_event(["CTRL-EVENT-CONNECTED",
                            "INTERWORKING-ALREADY-CONNECTED" ], timeout=15)
    if ev is None:
        raise Exception("Connection timed out")
    if "INTERWORKING-ALREADY-CONNECTED" in ev:
        raise Exception("No roam to higher priority network")
    if bssid2 not in ev:
        raise Exception("Unexpected network selected")

def test_ap_hs20_network_preference4(dev, apdev):
    """Hotspot 2.0 network selection with username vs. SIM credential"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    hostapd.add_ap(apdev[0]['ifname'], params)

    bssid2 = apdev[1]['bssid']
    params = hs20_ap_params(ssid="test-hs20b")
    params['hessid'] = bssid2
    params['anqp_3gpp_cell_net'] = "555,444"
    params['domain_name'] = "wlan.mnc444.mcc555.3gppnetwork.org"
    hostapd.add_ap(apdev[1]['ifname'], params)

    dev[0].hs20_enable()
    values = { 'realm': "example.com",
               'username': "hs20-test",
               'password': "password",
               'priority': "1" }
    dev[0].add_cred_values(values)
    values = { 'imsi': "555444-333222111",
               'eap': "SIM",
               'milenage': "5122250214c33e723a5dd523fc145fc0:981d464c7c52eb6e5036234984ad0bcf:000000000123" }
    id = dev[0].add_cred_values(values)

    dev[0].scan_for_bss(bssid, freq="2412")
    dev[0].scan_for_bss(bssid2, freq="2412")
    dev[0].request("INTERWORKING_SELECT auto freq=2412")
    ev = dev[0].wait_connected(timeout=15)
    if bssid not in ev:
        raise Exception("Unexpected network selected")

    dev[0].set_cred(id, "priority", "2")
    dev[0].request("INTERWORKING_SELECT auto freq=2412")
    ev = dev[0].wait_event(["CTRL-EVENT-CONNECTED",
                            "INTERWORKING-ALREADY-CONNECTED" ], timeout=15)
    if ev is None:
        raise Exception("Connection timed out")
    if "INTERWORKING-ALREADY-CONNECTED" in ev:
        raise Exception("No roam to higher priority network")
    if bssid2 not in ev:
        raise Exception("Unexpected network selected")

def test_ap_hs20_fetch_osu(dev, apdev):
    """Hotspot 2.0 OSU provider and icon fetch"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['hs20_icon'] = "128:80:zxx:image/png:w1fi_logo:w1fi_logo.png"
    params['osu_ssid'] = '"HS 2.0 OSU open"'
    params['osu_method_list'] = "1"
    params['osu_friendly_name'] = [ "eng:Test OSU", "fin:Testi-OSU" ]
    params['osu_icon'] = "w1fi_logo"
    params['osu_service_desc'] = [ "eng:Example services", "fin:Esimerkkipalveluja" ]
    params['osu_server_uri'] = "https://example.com/osu/"
    hostapd.add_ap(apdev[0]['ifname'], params)

    bssid2 = apdev[1]['bssid']
    params = hs20_ap_params(ssid="test-hs20b")
    params['hessid'] = bssid2
    params['hs20_icon'] = "128:80:zxx:image/png:w1fi_logo:w1fi_logo.png"
    params['osu_ssid'] = '"HS 2.0 OSU OSEN"'
    params['osu_method_list'] = "0"
    params['osu_nai'] = "osen@example.com"
    params['osu_friendly_name'] = [ "eng:Test2 OSU", "fin:Testi2-OSU" ]
    params['osu_icon'] = "w1fi_logo"
    params['osu_service_desc'] = [ "eng:Example services2", "fin:Esimerkkipalveluja2" ]
    params['osu_server_uri'] = "https://example.org/osu/"
    hostapd.add_ap(apdev[1]['ifname'], params)

    with open("w1fi_logo.png", "r") as f:
        orig_logo = f.read()
    dev[0].hs20_enable()
    dir = "/tmp/osu-fetch"
    if os.path.isdir(dir):
       files = [ f for f in os.listdir(dir) if f.startswith("osu-") ]
       for f in files:
           os.remove(dir + "/" + f)
    else:
        try:
            os.makedirs(dir)
        except:
            pass
    try:
        dev[1].scan_for_bss(bssid, freq="2412")
        dev[0].request("SET osu_dir " + dir)
        dev[0].request("FETCH_OSU")
        if "FAIL" not in dev[1].request("HS20_ICON_REQUEST foo w1fi_logo"):
            raise Exception("Invalid HS20_ICON_REQUEST accepted")
        if "OK" not in dev[1].request("HS20_ICON_REQUEST " + bssid + " w1fi_logo"):
            raise Exception("HS20_ICON_REQUEST failed")
        icons = 0
        while True:
            ev = dev[0].wait_event(["OSU provider fetch completed",
                                    "RX-HS20-ANQP-ICON"], timeout=15)
            if ev is None:
                raise Exception("Timeout on OSU fetch")
            if "OSU provider fetch completed" in ev:
                break
            if "RX-HS20-ANQP-ICON" in ev:
                with open(ev.split(' ')[1], "r") as f:
                    logo = f.read()
                    if logo == orig_logo:
                        icons += 1

        with open(dir + "/osu-providers.txt", "r") as f:
            prov = f.read()
            logger.debug("osu-providers.txt: " + prov)
        if "OSU-PROVIDER " + bssid not in prov:
            raise Exception("Missing OSU_PROVIDER(1)")
        if "OSU-PROVIDER " + bssid2 not in prov:
            raise Exception("Missing OSU_PROVIDER(2)")
    finally:
        files = [ f for f in os.listdir(dir) if f.startswith("osu-") ]
        for f in files:
            os.remove(dir + "/" + f)
        os.rmdir(dir)

    if icons != 2:
        raise Exception("Unexpected number of icons fetched")

    ev = dev[1].wait_event(["GAS-QUERY-START"], timeout=5)
    if ev is None:
        raise Exception("Timeout on GAS-QUERY-DONE")
    ev = dev[1].wait_event(["GAS-QUERY-DONE"], timeout=5)
    if ev is None:
        raise Exception("Timeout on GAS-QUERY-DONE")
    if "freq=2412 status_code=0 result=SUCCESS" not in ev:
        raise Exception("Unexpected GAS-QUERY-DONE: " + ev)
    ev = dev[1].wait_event(["RX-HS20-ANQP"], timeout=15)
    if ev is None:
        raise Exception("Timeout on icon fetch")
    if "Icon Binary File" not in ev:
        raise Exception("Unexpected ANQP element")

def test_ap_hs20_fetch_osu_stop(dev, apdev):
    """Hotspot 2.0 OSU provider fetch stopped"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['hs20_icon'] = "128:80:zxx:image/png:w1fi_logo:w1fi_logo.png"
    params['osu_ssid'] = '"HS 2.0 OSU open"'
    params['osu_method_list'] = "1"
    params['osu_friendly_name'] = [ "eng:Test OSU", "fin:Testi-OSU" ]
    params['osu_icon'] = "w1fi_logo"
    params['osu_service_desc'] = [ "eng:Example services", "fin:Esimerkkipalveluja" ]
    params['osu_server_uri'] = "https://example.com/osu/"
    hapd = hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    dir = "/tmp/osu-fetch"
    if os.path.isdir(dir):
       files = [ f for f in os.listdir(dir) if f.startswith("osu-") ]
       for f in files:
           os.remove(dir + "/" + f)
    else:
        try:
            os.makedirs(dir)
        except:
            pass
    try:
        dev[0].request("SET osu_dir " + dir)
        dev[0].request("SCAN freq=2412-2462")
        ev = dev[0].wait_event(["CTRL-EVENT-SCAN-STARTED"], timeout=10)
        if ev is None:
            raise Exception("Scan did not start")
        if "FAIL" not in dev[0].request("FETCH_OSU"):
            raise Exception("FETCH_OSU accepted while scanning")
        ev = dev[0].wait_event(["CTRL-EVENT-SCAN-RESULTS"], 10)
        if ev is None:
            raise Exception("Scan timed out")
        hapd.set("ext_mgmt_frame_handling", "1")
        dev[0].request("FETCH_ANQP")
        if "FAIL" not in dev[0].request("FETCH_OSU"):
            raise Exception("FETCH_OSU accepted while in FETCH_ANQP")
        dev[0].request("STOP_FETCH_ANQP")
        dev[0].wait_event(["GAS-QUERY-DONE"], timeout=5)
        dev[0].dump_monitor()
        hapd.dump_monitor()
        dev[0].request("INTERWORKING_SELECT freq=2412")
        for i in range(5):
            msg = hapd.mgmt_rx()
            if msg['subtype'] == 13:
                break
        if "FAIL" not in dev[0].request("FETCH_OSU"):
            raise Exception("FETCH_OSU accepted while in INTERWORKING_SELECT")
        ev = dev[0].wait_event(["INTERWORKING-AP", "INTERWORKING-NO-MATCH"],
                               timeout=15)
        if ev is None:
            raise Exception("Network selection timed out");

        dev[0].dump_monitor()
        if "OK" not in dev[0].request("FETCH_OSU"):
            raise Exception("FETCH_OSU failed")
        dev[0].request("CANCEL_FETCH_OSU")

        for i in range(15):
            time.sleep(0.5)
            if dev[0].get_driver_status_field("scan_state") == "SCAN_COMPLETED":
                break

        dev[0].dump_monitor()
        if "OK" not in dev[0].request("FETCH_OSU"):
            raise Exception("FETCH_OSU failed")
        if "FAIL" not in dev[0].request("FETCH_OSU"):
            raise Exception("FETCH_OSU accepted while in FETCH_OSU")
        ev = dev[0].wait_event(["GAS-QUERY-START"], 10)
        if ev is None:
            raise Exception("GAS timed out")
        if "FAIL" not in dev[0].request("FETCH_OSU"):
            raise Exception("FETCH_OSU accepted while in FETCH_OSU")
        dev[0].request("CANCEL_FETCH_OSU")
        ev = dev[0].wait_event(["GAS-QUERY-DONE"], 10)
        if ev is None:
            raise Exception("GAS event timed out after CANCEL_FETCH_OSU")
    finally:
        files = [ f for f in os.listdir(dir) if f.startswith("osu-") ]
        for f in files:
            os.remove(dir + "/" + f)
        os.rmdir(dir)

def test_ap_hs20_ft(dev, apdev):
    """Hotspot 2.0 connection with FT"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['wpa_key_mgmt'] = "FT-EAP"
    params['nas_identifier'] = "nas1.w1.fi"
    params['r1_key_holder'] = "000102030405"
    params["mobility_domain"] = "a1b2"
    params["reassociation_deadline"] = "1000"
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    id = dev[0].add_cred_values({ 'realm': "example.com",
                                  'username': "hs20-test",
                                  'password': "password",
                                  'ca_cert': "auth_serv/ca.pem",
                                  'domain': "example.com",
                                  'update_identifier': "1234" })
    interworking_select(dev[0], bssid, "home", freq="2412")
    interworking_connect(dev[0], bssid, "TTLS")

def test_ap_hs20_remediation_sql(dev, apdev, params):
    """Hotspot 2.0 connection and remediation required using SQLite for user DB"""
    try:
        import sqlite3
    except ImportError:
        raise HwsimSkip("No sqlite3 module available")
    dbfile = os.path.join(params['logdir'], "eap-user.db")
    try:
        os.remove(dbfile)
    except:
        pass
    con = sqlite3.connect(dbfile)
    with con:
        cur = con.cursor()
        cur.execute("CREATE TABLE users(identity TEXT PRIMARY KEY, methods TEXT, password TEXT, remediation TEXT, phase2 INTEGER)")
        cur.execute("CREATE TABLE wildcards(identity TEXT PRIMARY KEY, methods TEXT)")
        cur.execute("INSERT INTO users(identity,methods,password,phase2,remediation) VALUES ('user-mschapv2','TTLS-MSCHAPV2','password',1,'user')")
        cur.execute("INSERT INTO wildcards(identity,methods) VALUES ('','TTLS,TLS')")
        cur.execute("CREATE TABLE authlog(timestamp TEXT, session TEXT, nas_ip TEXT, username TEXT, note TEXT)")

    try:
        params = { "ssid": "as", "beacon_int": "2000",
                   "radius_server_clients": "auth_serv/radius_clients.conf",
                   "radius_server_auth_port": '18128',
                   "eap_server": "1",
                   "eap_user_file": "sqlite:" + dbfile,
                   "ca_cert": "auth_serv/ca.pem",
                   "server_cert": "auth_serv/server.pem",
                   "private_key": "auth_serv/server.key",
                   "subscr_remediation_url": "https://example.org/",
                   "subscr_remediation_method": "1" }
        hostapd.add_ap(apdev[1]['ifname'], params)

        bssid = apdev[0]['bssid']
        params = hs20_ap_params()
        params['auth_server_port'] = "18128"
        hostapd.add_ap(apdev[0]['ifname'], params)

        dev[0].request("SET pmf 1")
        dev[0].hs20_enable()
        id = dev[0].add_cred_values({ 'realm': "example.com",
                                      'username': "user-mschapv2",
                                      'password': "password",
                                      'ca_cert': "auth_serv/ca.pem" })
        interworking_select(dev[0], bssid, freq="2412")
        interworking_connect(dev[0], bssid, "TTLS")
        ev = dev[0].wait_event(["HS20-SUBSCRIPTION-REMEDIATION"], timeout=5)
        if ev is None:
            raise Exception("Timeout on subscription remediation notice")
        if " 1 https://example.org/" not in ev:
            raise Exception("Unexpected subscription remediation event contents")

        with con:
            cur = con.cursor()
            cur.execute("SELECT * from authlog")
            rows = cur.fetchall()
            if len(rows) < 1:
                raise Exception("No authlog entries")

    finally:
        os.remove(dbfile)
        dev[0].request("SET pmf 0")

def test_ap_hs20_external_selection(dev, apdev):
    """Hotspot 2.0 connection using external network selection and creation"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['hessid'] = bssid
    params['disable_dgaf'] = '1'
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    dev[0].connect("test-hs20", proto="RSN", key_mgmt="WPA-EAP", eap="TTLS",
                   identity="hs20-test", password="password",
                   ca_cert="auth_serv/ca.pem", phase2="auth=MSCHAPV2",
                   scan_freq="2412", update_identifier="54321")
    if dev[0].get_status_field("hs20") != "2":
        raise Exception("Unexpected hs20 indication")

def test_ap_hs20_random_mac_addr(dev, apdev):
    """Hotspot 2.0 connection with random MAC address"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['hessid'] = bssid
    params['disable_dgaf'] = '1'
    hapd = hostapd.add_ap(apdev[0]['ifname'], params)

    wpas = WpaSupplicant(global_iface='/tmp/wpas-wlan5')
    wpas.interface_add("wlan5")
    addr = wpas.p2p_interface_addr()
    wpas.request("SET mac_addr 1")
    wpas.request("SET preassoc_mac_addr 1")
    wpas.request("SET rand_addr_lifetime 60")
    wpas.hs20_enable()
    wpas.flush_scan_cache()
    id = wpas.add_cred_values({ 'realm': "example.com",
                                  'username': "hs20-test",
                                  'password': "password",
                                  'ca_cert': "auth_serv/ca.pem",
                                  'domain': "example.com",
                                  'update_identifier': "1234" })
    interworking_select(wpas, bssid, "home", freq="2412")
    interworking_connect(wpas, bssid, "TTLS")
    addr1 = wpas.get_driver_status_field("addr")
    if addr == addr1:
        raise Exception("Did not use random MAC address")

    sta = hapd.get_sta(addr)
    if sta['addr'] != "FAIL":
        raise Exception("Unexpected STA association with permanent address")
    sta = hapd.get_sta(addr1)
    if sta['addr'] != addr1:
        raise Exception("STA association with random address not found")

def test_ap_hs20_multi_network_and_cred_removal(dev, apdev):
    """Multiple networks and cred removal"""
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['nai_realm'] = [ "0,example.com,25[3:26]"]
    hapd = hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].add_network()
    dev[0].hs20_enable()
    id = dev[0].add_cred_values({ 'realm': "example.com",
                                  'username': "user",
                                  'password': "password" })
    interworking_select(dev[0], bssid, freq="2412")
    interworking_connect(dev[0], bssid, "PEAP")
    dev[0].add_network()

    dev[0].request("DISCONNECT")
    dev[0].wait_disconnected(timeout=10)

    hapd.disable()
    hapd.set("ssid", "another ssid")
    hapd.enable()

    interworking_select(dev[0], bssid, freq="2412")
    interworking_connect(dev[0], bssid, "PEAP")
    dev[0].add_network()
    if len(dev[0].list_networks()) != 5:
        raise Exception("Unexpected number of networks prior to remove_crec")

    dev[0].dump_monitor()
    dev[0].remove_cred(id)
    if len(dev[0].list_networks()) != 3:
        raise Exception("Unexpected number of networks after to remove_crec")
    dev[0].wait_disconnected(timeout=10)

def _test_ap_hs20_proxyarp(dev, apdev):
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['hessid'] = bssid
    params['disable_dgaf'] = '0'
    params['proxy_arp'] = '1'
    hapd = hostapd.add_ap(apdev[0]['ifname'], params, no_enable=True)
    if "OK" in hapd.request("ENABLE"):
        raise Exception("Incomplete hostapd configuration was accepted")
    hapd.set("ap_isolate", "1")
    if "OK" in hapd.request("ENABLE"):
        raise Exception("Incomplete hostapd configuration was accepted")
    hapd.set('bridge', 'ap-br0')
    hapd.dump_monitor()
    try:
        hapd.enable()
    except:
        # For now, do not report failures due to missing kernel support
        raise HwsimSkip("Could not start hostapd - assume proxyarp not supported in kernel version")
    ev = hapd.wait_event(["AP-ENABLED", "AP-DISABLED"], timeout=10)
    if ev is None:
        raise Exception("AP startup timed out")
    if "AP-ENABLED" not in ev:
        raise Exception("AP startup failed")

    dev[0].hs20_enable()
    subprocess.call(['brctl', 'setfd', 'ap-br0', '0'])
    subprocess.call(['ip', 'link', 'set', 'dev', 'ap-br0', 'up'])

    id = dev[0].add_cred_values({ 'realm': "example.com",
                                  'username': "hs20-test",
                                  'password': "password",
                                  'ca_cert': "auth_serv/ca.pem",
                                  'domain': "example.com",
                                  'update_identifier': "1234" })
    interworking_select(dev[0], bssid, "home", freq="2412")
    interworking_connect(dev[0], bssid, "TTLS")

    dev[1].connect("test-hs20", key_mgmt="WPA-EAP", eap="TTLS",
                   identity="hs20-test", password="password",
                   ca_cert="auth_serv/ca.pem", phase2="auth=MSCHAPV2",
                   scan_freq="2412")
    time.sleep(0.1)

    addr0 = dev[0].p2p_interface_addr()
    addr1 = dev[1].p2p_interface_addr()

    src_ll_opt0 = "\x01\x01" + binascii.unhexlify(addr0.replace(':',''))
    src_ll_opt1 = "\x01\x01" + binascii.unhexlify(addr1.replace(':',''))

    pkt = build_ns(src_ll=addr0, ip_src="aaaa:bbbb:cccc::2",
                   ip_dst="ff02::1:ff00:2", target="aaaa:bbbb:cccc::2",
                   opt=src_ll_opt0)
    if "OK" not in dev[0].request("DATA_TEST_FRAME " + binascii.hexlify(pkt)):
        raise Exception("DATA_TEST_FRAME failed")

    pkt = build_ns(src_ll=addr1, ip_src="aaaa:bbbb:dddd::2",
                   ip_dst="ff02::1:ff00:2", target="aaaa:bbbb:dddd::2",
                   opt=src_ll_opt1)
    if "OK" not in dev[1].request("DATA_TEST_FRAME " + binascii.hexlify(pkt)):
        raise Exception("DATA_TEST_FRAME failed")

    pkt = build_ns(src_ll=addr1, ip_src="aaaa:bbbb:eeee::2",
                   ip_dst="ff02::1:ff00:2", target="aaaa:bbbb:eeee::2",
                   opt=src_ll_opt1)
    if "OK" not in dev[1].request("DATA_TEST_FRAME " + binascii.hexlify(pkt)):
        raise Exception("DATA_TEST_FRAME failed")

    matches = get_permanent_neighbors("ap-br0")
    logger.info("After connect: " + str(matches))
    if len(matches) != 3:
        raise Exception("Unexpected number of neighbor entries after connect")
    if 'aaaa:bbbb:cccc::2 dev ap-br0 lladdr 02:00:00:00:00:00 PERMANENT' not in matches:
        raise Exception("dev0 addr missing")
    if 'aaaa:bbbb:dddd::2 dev ap-br0 lladdr 02:00:00:00:01:00 PERMANENT' not in matches:
        raise Exception("dev1 addr(1) missing")
    if 'aaaa:bbbb:eeee::2 dev ap-br0 lladdr 02:00:00:00:01:00 PERMANENT' not in matches:
        raise Exception("dev1 addr(2) missing")
    dev[0].request("DISCONNECT")
    dev[1].request("DISCONNECT")
    time.sleep(0.5)
    matches = get_permanent_neighbors("ap-br0")
    logger.info("After disconnect: " + str(matches))
    if len(matches) > 0:
        raise Exception("Unexpected neighbor entries after disconnect")

def test_ap_hs20_hidden_ssid_in_scan_res(dev, apdev):
    """Hotspot 2.0 connection with hidden SSId in scan results"""
    bssid = apdev[0]['bssid']

    hapd = hostapd.add_ap(apdev[0]['ifname'], { "ssid": 'secret',
                                                "ignore_broadcast_ssid": "1" })
    dev[0].scan_for_bss(bssid, freq=2412)
    hapd.disable()
    hapd_global = hostapd.HostapdGlobal()
    hapd_global.flush()
    hapd_global.remove(apdev[0]['ifname'])

    params = hs20_ap_params()
    params['hessid'] = bssid
    hostapd.add_ap(apdev[0]['ifname'], params)

    dev[0].hs20_enable()
    id = dev[0].add_cred_values({ 'realm': "example.com",
                                  'username': "hs20-test",
                                  'password': "password",
                                  'ca_cert': "auth_serv/ca.pem",
                                  'domain': "example.com" })
    interworking_select(dev[0], bssid, "home", freq="2412")
    interworking_connect(dev[0], bssid, "TTLS")

    # clear BSS table to avoid issues in following test cases
    dev[0].request("DISCONNECT")
    dev[0].wait_disconnected()
    dev[0].flush_scan_cache()

def test_ap_hs20_proxyarp(dev, apdev):
    """Hotspot 2.0 and ProxyARP"""
    try:
        _test_ap_hs20_proxyarp(dev, apdev)
    finally:
        subprocess.call(['ip', 'link', 'set', 'dev', 'ap-br0', 'down'],
                        stderr=open('/dev/null', 'w'))
        subprocess.call(['brctl', 'delbr', 'ap-br0'],
                        stderr=open('/dev/null', 'w'))

def _test_ap_hs20_proxyarp_dgaf(dev, apdev, disabled):
    bssid = apdev[0]['bssid']
    params = hs20_ap_params()
    params['hessid'] = bssid
    params['disable_dgaf'] = '1' if disabled else '0'
    params['proxy_arp'] = '1'
    params['ap_isolate'] = '1'
    params['bridge'] = 'ap-br0'
    hapd = hostapd.add_ap(apdev[0]['ifname'], params, no_enable=True)
    try:
        hapd.enable()
    except:
        # For now, do not report failures due to missing kernel support
        raise HwsimSkip("Could not start hostapd - assume proxyarp not supported in kernel version")
    ev = hapd.wait_event(["AP-ENABLED"], timeout=10)
    if ev is None:
        raise Exception("AP startup timed out")

    dev[0].hs20_enable()
    subprocess.call(['brctl', 'setfd', 'ap-br0', '0'])
    subprocess.call(['ip', 'link', 'set', 'dev', 'ap-br0', 'up'])

    id = dev[0].add_cred_values({ 'realm': "example.com",
                                  'username': "hs20-test",
                                  'password': "password",
                                  'ca_cert': "auth_serv/ca.pem",
                                  'domain': "example.com",
                                  'update_identifier': "1234" })
    interworking_select(dev[0], bssid, "home", freq="2412")
    interworking_connect(dev[0], bssid, "TTLS")

    dev[1].connect("test-hs20", key_mgmt="WPA-EAP", eap="TTLS",
                   identity="hs20-test", password="password",
                   ca_cert="auth_serv/ca.pem", phase2="auth=MSCHAPV2",
                   scan_freq="2412")
    time.sleep(0.1)

    addr0 = dev[0].p2p_interface_addr()

    src_ll_opt0 = "\x01\x01" + binascii.unhexlify(addr0.replace(':',''))

    pkt = build_ns(src_ll=addr0, ip_src="aaaa:bbbb:cccc::2",
                   ip_dst="ff02::1:ff00:2", target="aaaa:bbbb:cccc::2",
                   opt=src_ll_opt0)
    if "OK" not in dev[0].request("DATA_TEST_FRAME " + binascii.hexlify(pkt)):
        raise Exception("DATA_TEST_FRAME failed")

    pkt = build_ra(src_ll=apdev[0]['bssid'], ip_src="aaaa:bbbb:cccc::33",
                   ip_dst="ff01::1")
    if "OK" not in hapd.request("DATA_TEST_FRAME ifname=ap-br0 " + binascii.hexlify(pkt)):
        raise Exception("DATA_TEST_FRAME failed")

    pkt = build_na(src_ll=apdev[0]['bssid'], ip_src="aaaa:bbbb:cccc::44",
                   ip_dst="ff01::1", target="aaaa:bbbb:cccc::55")
    if "OK" not in hapd.request("DATA_TEST_FRAME ifname=ap-br0 " + binascii.hexlify(pkt)):
        raise Exception("DATA_TEST_FRAME failed")

    pkt = build_dhcp_ack(dst_ll="ff:ff:ff:ff:ff:ff", src_ll=bssid,
                         ip_src="192.168.1.1", ip_dst="255.255.255.255",
                         yiaddr="192.168.1.123", chaddr=addr0)
    if "OK" not in hapd.request("DATA_TEST_FRAME ifname=ap-br0 " + binascii.hexlify(pkt)):
        raise Exception("DATA_TEST_FRAME failed")
    # another copy for additional code coverage
    pkt = build_dhcp_ack(dst_ll=addr0, src_ll=bssid,
                         ip_src="192.168.1.1", ip_dst="255.255.255.255",
                         yiaddr="192.168.1.123", chaddr=addr0)
    if "OK" not in hapd.request("DATA_TEST_FRAME ifname=ap-br0 " + binascii.hexlify(pkt)):
        raise Exception("DATA_TEST_FRAME failed")

    matches = get_permanent_neighbors("ap-br0")
    logger.info("After connect: " + str(matches))
    if len(matches) != 2:
        raise Exception("Unexpected number of neighbor entries after connect")
    if 'aaaa:bbbb:cccc::2 dev ap-br0 lladdr 02:00:00:00:00:00 PERMANENT' not in matches:
        raise Exception("dev0 addr missing")
    if '192.168.1.123 dev ap-br0 lladdr 02:00:00:00:00:00 PERMANENT' not in matches:
        raise Exception("dev0 IPv4 addr missing")
    dev[0].request("DISCONNECT")
    dev[1].request("DISCONNECT")
    time.sleep(0.5)
    matches = get_permanent_neighbors("ap-br0")
    logger.info("After disconnect: " + str(matches))
    if len(matches) > 0:
        raise Exception("Unexpected neighbor entries after disconnect")

def test_ap_hs20_proxyarp_disable_dgaf(dev, apdev):
    """Hotspot 2.0 and ProxyARP with DGAF disabled"""
    try:
        _test_ap_hs20_proxyarp_dgaf(dev, apdev, True)
    finally:
        subprocess.call(['ip', 'link', 'set', 'dev', 'ap-br0', 'down'],
                        stderr=open('/dev/null', 'w'))
        subprocess.call(['brctl', 'delbr', 'ap-br0'],
                        stderr=open('/dev/null', 'w'))

def test_ap_hs20_proxyarp_enable_dgaf(dev, apdev):
    """Hotspot 2.0 and ProxyARP with DGAF enabled"""
    try:
        _test_ap_hs20_proxyarp_dgaf(dev, apdev, False)
    finally:
        subprocess.call(['ip', 'link', 'set', 'dev', 'ap-br0', 'down'],
                        stderr=open('/dev/null', 'w'))
        subprocess.call(['brctl', 'delbr', 'ap-br0'],
                        stderr=open('/dev/null', 'w'))

def ip_checksum(buf):
    sum = 0
    if len(buf) & 0x01:
        buf += '\0x00'
    for i in range(0, len(buf), 2):
        val, = struct.unpack('H', buf[i:i+2])
        sum += val
    while (sum >> 16):
        sum = (sum & 0xffff) + (sum >> 16)
    return struct.pack('H', ~sum & 0xffff)

def ipv6_solicited_node_mcaddr(target):
    prefix = socket.inet_pton(socket.AF_INET6, "ff02::1:ff00:0")
    mask = socket.inet_pton(socket.AF_INET6, "::ff:ffff")
    _target = socket.inet_pton(socket.AF_INET6, target)
    p = struct.unpack('4I', prefix)
    m = struct.unpack('4I', mask)
    t = struct.unpack('4I', _target)
    res = (p[0] | (t[0] & m[0]),
           p[1] | (t[1] & m[1]),
           p[2] | (t[2] & m[2]),
           p[3] | (t[3] & m[3]))
    return socket.inet_ntop(socket.AF_INET6, struct.pack('4I', *res))

def build_icmpv6(ipv6_addrs, type, code, payload):
    start = struct.pack("BB", type, code)
    end = payload
    icmp = start + '\x00\x00' + end
    pseudo = ipv6_addrs + struct.pack(">LBBBB", len(icmp), 0, 0, 0, 58)
    csum = ip_checksum(pseudo + icmp)
    return start + csum + end

def build_ra(src_ll, ip_src, ip_dst, cur_hop_limit=0, router_lifetime=0,
             reachable_time=0, retrans_timer=0, opt=None):
    link_mc = binascii.unhexlify("3333ff000002")
    _src_ll = binascii.unhexlify(src_ll.replace(':',''))
    proto = '\x86\xdd'
    ehdr = link_mc + _src_ll + proto
    _ip_src = socket.inet_pton(socket.AF_INET6, ip_src)
    _ip_dst = socket.inet_pton(socket.AF_INET6, ip_dst)

    adv = struct.pack('>BBHLL', cur_hop_limit, 0, router_lifetime,
                      reachable_time, retrans_timer)
    if opt:
        payload = adv + opt
    else:
        payload = adv
    icmp = build_icmpv6(_ip_src + _ip_dst, 134, 0, payload)

    ipv6 = struct.pack('>BBBBHBB', 0x60, 0, 0, 0, len(icmp), 58, 255)
    ipv6 += _ip_src + _ip_dst

    return ehdr + ipv6 + icmp

def build_ns(src_ll, ip_src, ip_dst, target, opt=None):
    link_mc = binascii.unhexlify("3333ff000002")
    _src_ll = binascii.unhexlify(src_ll.replace(':',''))
    proto = '\x86\xdd'
    ehdr = link_mc + _src_ll + proto
    _ip_src = socket.inet_pton(socket.AF_INET6, ip_src)
    if ip_dst is None:
        ip_dst = ipv6_solicited_node_mcaddr(target)
    _ip_dst = socket.inet_pton(socket.AF_INET6, ip_dst)

    reserved = '\x00\x00\x00\x00'
    _target = socket.inet_pton(socket.AF_INET6, target)
    if opt:
        payload = reserved + _target + opt
    else:
        payload = reserved + _target
    icmp = build_icmpv6(_ip_src + _ip_dst, 135, 0, payload)

    ipv6 = struct.pack('>BBBBHBB', 0x60, 0, 0, 0, len(icmp), 58, 255)
    ipv6 += _ip_src + _ip_dst

    return ehdr + ipv6 + icmp

def send_ns(dev, src_ll=None, target=None, ip_src=None, ip_dst=None, opt=None,
            hapd_bssid=None):
    if hapd_bssid:
        if src_ll is None:
            src_ll = hapd_bssid
        cmd = "DATA_TEST_FRAME ifname=ap-br0 "
    else:
        if src_ll is None:
            src_ll = dev.p2p_interface_addr()
        cmd = "DATA_TEST_FRAME "

    if opt is None:
        opt = "\x01\x01" + binascii.unhexlify(src_ll.replace(':',''))

    pkt = build_ns(src_ll=src_ll, ip_src=ip_src, ip_dst=ip_dst, target=target,
                   opt=opt)
    if "OK" not in dev.request(cmd + binascii.hexlify(pkt)):
        raise Exception("DATA_TEST_FRAME failed")

def build_na(src_ll, ip_src, ip_dst, target, opt=None):
    link_mc = binascii.unhexlify("3333ff000002")
    _src_ll = binascii.unhexlify(src_ll.replace(':',''))
    proto = '\x86\xdd'
    ehdr = link_mc + _src_ll + proto
    _ip_src = socket.inet_pton(socket.AF_INET6, ip_src)
    _ip_dst = socket.inet_pton(socket.AF_INET6, ip_dst)

    reserved = '\x00\x00\x00\x00'
    _target = socket.inet_pton(socket.AF_INET6, target)
    if opt:
        payload = reserved + _target + opt
    else:
        payload = reserved + _target
    icmp = build_icmpv6(_ip_src + _ip_dst, 136, 0, payload)

    ipv6 = struct.pack('>BBBBHBB', 0x60, 0, 0, 0, len(icmp), 58, 255)
    ipv6 += _ip_src + _ip_dst

    return ehdr + ipv6 + icmp

def send_na(dev, src_ll=None, target=None, ip_src=None, ip_dst=None, opt=None,
            hapd_bssid=None):
    if hapd_bssid:
        if src_ll is None:
            src_ll = hapd_bssid
        cmd = "DATA_TEST_FRAME ifname=ap-br0 "
    else:
        if src_ll is None:
            src_ll = dev.p2p_interface_addr()
        cmd = "DATA_TEST_FRAME "

    pkt = build_na(src_ll=src_ll, ip_src=ip_src, ip_dst=ip_dst, target=target,
                   opt=opt)
    if "OK" not in dev.request(cmd + binascii.hexlify(pkt)):
        raise Exception("DATA_TEST_FRAME failed")

def build_dhcp_ack(dst_ll, src_ll, ip_src, ip_dst, yiaddr, chaddr,
                   subnet_mask="255.255.255.0", truncated_opt=False,
                   wrong_magic=False, force_tot_len=None, no_dhcp=False):
    _dst_ll = binascii.unhexlify(dst_ll.replace(':',''))
    _src_ll = binascii.unhexlify(src_ll.replace(':',''))
    proto = '\x08\x00'
    ehdr = _dst_ll + _src_ll + proto
    _ip_src = socket.inet_pton(socket.AF_INET, ip_src)
    _ip_dst = socket.inet_pton(socket.AF_INET, ip_dst)
    _subnet_mask = socket.inet_pton(socket.AF_INET, subnet_mask)

    _ciaddr = '\x00\x00\x00\x00'
    _yiaddr = socket.inet_pton(socket.AF_INET, yiaddr)
    _siaddr = '\x00\x00\x00\x00'
    _giaddr = '\x00\x00\x00\x00'
    _chaddr = binascii.unhexlify(chaddr.replace(':','') + "00000000000000000000")
    payload = struct.pack('>BBBBL3BB', 2, 1, 6, 0, 12345, 0, 0, 0, 0)
    payload += _ciaddr + _yiaddr + _siaddr + _giaddr + _chaddr + 192*'\x00'
    # magic
    if wrong_magic:
        payload += '\x63\x82\x53\x00'
    else:
        payload += '\x63\x82\x53\x63'
    if truncated_opt:
        payload += '\x22\xff\x00'
    # Option: DHCP Message Type = ACK
    payload += '\x35\x01\x05'
    # Pad Option
    payload += '\x00'
    # Option: Subnet Mask
    payload += '\x01\x04' + _subnet_mask
    # Option: Time Offset
    payload += struct.pack('>BBL', 2, 4, 0)
    # End Option
    payload += '\xff'
    # Pad Option
    payload += '\x00\x00\x00\x00'

    if no_dhcp:
        payload = struct.pack('>BBBBL3BB', 2, 1, 6, 0, 12345, 0, 0, 0, 0)
        payload += _ciaddr + _yiaddr + _siaddr + _giaddr + _chaddr + 192*'\x00'

    udp = struct.pack('>HHHH', 67, 68, 8 + len(payload), 0) + payload

    if force_tot_len:
        tot_len = force_tot_len
    else:
        tot_len = 20 + len(udp)
    start = struct.pack('>BBHHBBBB', 0x45, 0, tot_len, 0, 0, 0, 128, 17)
    ipv4 = start + '\x00\x00' + _ip_src + _ip_dst
    csum = ip_checksum(ipv4)
    ipv4 = start + csum + _ip_src + _ip_dst

    return ehdr + ipv4 + udp

def build_arp(dst_ll, src_ll, opcode, sender_mac, sender_ip,
              target_mac, target_ip):
    _dst_ll = binascii.unhexlify(dst_ll.replace(':',''))
    _src_ll = binascii.unhexlify(src_ll.replace(':',''))
    proto = '\x08\x06'
    ehdr = _dst_ll + _src_ll + proto

    _sender_mac = binascii.unhexlify(sender_mac.replace(':',''))
    _sender_ip = socket.inet_pton(socket.AF_INET, sender_ip)
    _target_mac = binascii.unhexlify(target_mac.replace(':',''))
    _target_ip = socket.inet_pton(socket.AF_INET, target_ip)

    arp = struct.pack('>HHBBH', 1, 0x0800, 6, 4, opcode)
    arp += _sender_mac + _sender_ip
    arp += _target_mac + _target_ip

    return ehdr + arp

def send_arp(dev, dst_ll="ff:ff:ff:ff:ff:ff", src_ll=None, opcode=1,
             sender_mac=None, sender_ip="0.0.0.0",
             target_mac="00:00:00:00:00:00", target_ip="0.0.0.0",
             hapd_bssid=None):
    if hapd_bssid:
        if src_ll is None:
            src_ll = hapd_bssid
        if sender_mac is None:
            sender_mac = hapd_bssid
        cmd = "DATA_TEST_FRAME ifname=ap-br0 "
    else:
        if src_ll is None:
            src_ll = dev.p2p_interface_addr()
        if sender_mac is None:
            sender_mac = dev.p2p_interface_addr()
        cmd = "DATA_TEST_FRAME "

    pkt = build_arp(dst_ll=dst_ll, src_ll=src_ll, opcode=opcode,
                    sender_mac=sender_mac, sender_ip=sender_ip,
                    target_mac=target_mac, target_ip=target_ip)
    if "OK" not in dev.request(cmd + binascii.hexlify(pkt)):
        raise Exception("DATA_TEST_FRAME failed")

def get_permanent_neighbors(ifname):
    cmd = subprocess.Popen(['ip', 'nei'], stdout=subprocess.PIPE)
    res = cmd.stdout.read()
    cmd.stdout.close()
    return [ line for line in res.splitlines() if "PERMANENT" in line and ifname in line ]

def _test_proxyarp_open(dev, apdev, params):
    cap_br = os.path.join(params['logdir'], "proxyarp_open.ap-br0.pcap")
    cap_dev0 = os.path.join(params['logdir'], "proxyarp_open.%s.pcap" % dev[0].ifname)
    cap_dev1 = os.path.join(params['logdir'], "proxyarp_open.%s.pcap" % dev[1].ifname)

    bssid = apdev[0]['bssid']
    params = { 'ssid': 'open' }
    params['proxy_arp'] = '1'
    hapd = hostapd.add_ap(apdev[0]['ifname'], params, no_enable=True)
    hapd.set("ap_isolate", "1")
    hapd.set('bridge', 'ap-br0')
    hapd.dump_monitor()
    try:
        hapd.enable()
    except:
        # For now, do not report failures due to missing kernel support
        raise HwsimSkip("Could not start hostapd - assume proxyarp not supported in kernel version")
    ev = hapd.wait_event(["AP-ENABLED", "AP-DISABLED"], timeout=10)
    if ev is None:
        raise Exception("AP startup timed out")
    if "AP-ENABLED" not in ev:
        raise Exception("AP startup failed")

    subprocess.call(['brctl', 'setfd', 'ap-br0', '0'])
    subprocess.call(['ip', 'link', 'set', 'dev', 'ap-br0', 'up'])

    for chain in [ 'FORWARD', 'OUTPUT' ]:
        subprocess.call(['ebtables', '-A', chain, '-p', 'ARP',
                         '-d', 'Broadcast', '-o', apdev[0]['ifname'],
                         '-j', 'DROP'])
        subprocess.call(['ebtables', '-A', chain, '-d', 'Multicast',
                         '-p', 'IPv6', '--ip6-protocol', 'ipv6-icmp',
                         '--ip6-icmp-type', 'neighbor-solicitation',
                         '-o', apdev[0]['ifname'], '-j', 'DROP'])
        subprocess.call(['ebtables', '-A', chain, '-d', 'Multicast',
                         '-p', 'IPv6', '--ip6-protocol', 'ipv6-icmp',
                         '--ip6-icmp-type', 'neighbor-advertisement',
                         '-o', apdev[0]['ifname'], '-j', 'DROP'])
        subprocess.call(['ebtables', '-A', chain,
                         '-p', 'IPv6', '--ip6-protocol', 'ipv6-icmp',
                         '--ip6-icmp-type', 'router-solicitation',
                         '-o', apdev[0]['ifname'], '-j', 'DROP'])
        # Multicast Listener Report Message
        subprocess.call(['ebtables', '-A', chain, '-d', 'Multicast',
                         '-p', 'IPv6', '--ip6-protocol', 'ipv6-icmp',
                         '--ip6-icmp-type', '143',
                         '-o', apdev[0]['ifname'], '-j', 'DROP'])

    cmd = {}
    cmd[0] = subprocess.Popen(['tcpdump', '-p', '-U', '-i', 'ap-br0',
                               '-w', cap_br, '-s', '2000'],
                              stderr=open('/dev/null', 'w'))
    cmd[1] = subprocess.Popen(['tcpdump', '-p', '-U', '-i', dev[0].ifname,
                               '-w', cap_dev0, '-s', '2000'],
                              stderr=open('/dev/null', 'w'))
    cmd[2] = subprocess.Popen(['tcpdump', '-p', '-U', '-i', dev[1].ifname,
                               '-w', cap_dev1, '-s', '2000'],
                              stderr=open('/dev/null', 'w'))

    dev[0].connect("open", key_mgmt="NONE", scan_freq="2412")
    dev[1].connect("open", key_mgmt="NONE", scan_freq="2412")
    time.sleep(0.1)

    addr0 = dev[0].p2p_interface_addr()
    addr1 = dev[1].p2p_interface_addr()

    src_ll_opt0 = "\x01\x01" + binascii.unhexlify(addr0.replace(':',''))
    src_ll_opt1 = "\x01\x01" + binascii.unhexlify(addr1.replace(':',''))

    # DAD NS
    send_ns(dev[0], ip_src="::", target="aaaa:bbbb:cccc::2")

    send_ns(dev[0], ip_src="aaaa:bbbb:cccc::2", target="aaaa:bbbb:cccc::2")
    # test frame without source link-layer address option
    send_ns(dev[0], ip_src="aaaa:bbbb:cccc::2", target="aaaa:bbbb:cccc::2",
            opt='')
    # test frame with bogus option
    send_ns(dev[0], ip_src="aaaa:bbbb:cccc::2", target="aaaa:bbbb:cccc::2",
            opt="\x70\x01\x01\x02\x03\x04\x05\x05")
    # test frame with truncated source link-layer address option
    send_ns(dev[0], ip_src="aaaa:bbbb:cccc::2", target="aaaa:bbbb:cccc::2",
            opt="\x01\x01\x01\x02\x03\x04")
    # test frame with foreign source link-layer address option
    send_ns(dev[0], ip_src="aaaa:bbbb:cccc::2", target="aaaa:bbbb:cccc::2",
            opt="\x01\x01\x01\x02\x03\x04\x05\x06")

    send_ns(dev[1], ip_src="aaaa:bbbb:dddd::2", target="aaaa:bbbb:dddd::2")

    send_ns(dev[1], ip_src="aaaa:bbbb:eeee::2", target="aaaa:bbbb:eeee::2")
    # another copy for additional code coverage
    send_ns(dev[1], ip_src="aaaa:bbbb:eeee::2", target="aaaa:bbbb:eeee::2")

    pkt = build_dhcp_ack(dst_ll="ff:ff:ff:ff:ff:ff", src_ll=bssid,
                         ip_src="192.168.1.1", ip_dst="255.255.255.255",
                         yiaddr="192.168.1.124", chaddr=addr0)
    if "OK" not in hapd.request("DATA_TEST_FRAME ifname=ap-br0 " + binascii.hexlify(pkt)):
        raise Exception("DATA_TEST_FRAME failed")
    # Change address and verify unicast
    pkt = build_dhcp_ack(dst_ll=addr0, src_ll=bssid,
                         ip_src="192.168.1.1", ip_dst="255.255.255.255",
                         yiaddr="192.168.1.123", chaddr=addr0)
    if "OK" not in hapd.request("DATA_TEST_FRAME ifname=ap-br0 " + binascii.hexlify(pkt)):
        raise Exception("DATA_TEST_FRAME failed")

    # Not-associated client MAC address
    pkt = build_dhcp_ack(dst_ll="ff:ff:ff:ff:ff:ff", src_ll=bssid,
                         ip_src="192.168.1.1", ip_dst="255.255.255.255",
                         yiaddr="192.168.1.125", chaddr="22:33:44:55:66:77")
    if "OK" not in hapd.request("DATA_TEST_FRAME ifname=ap-br0 " + binascii.hexlify(pkt)):
        raise Exception("DATA_TEST_FRAME failed")

    # No IP address
    pkt = build_dhcp_ack(dst_ll=addr1, src_ll=bssid,
                         ip_src="192.168.1.1", ip_dst="255.255.255.255",
                         yiaddr="0.0.0.0", chaddr=addr1)
    if "OK" not in hapd.request("DATA_TEST_FRAME ifname=ap-br0 " + binascii.hexlify(pkt)):
        raise Exception("DATA_TEST_FRAME failed")

    # Zero subnet mask
    pkt = build_dhcp_ack(dst_ll=addr1, src_ll=bssid,
                         ip_src="192.168.1.1", ip_dst="255.255.255.255",
                         yiaddr="192.168.1.126", chaddr=addr1,
                         subnet_mask="0.0.0.0")
    if "OK" not in hapd.request("DATA_TEST_FRAME ifname=ap-br0 " + binascii.hexlify(pkt)):
        raise Exception("DATA_TEST_FRAME failed")

    # Truncated option
    pkt = build_dhcp_ack(dst_ll=addr1, src_ll=bssid,
                         ip_src="192.168.1.1", ip_dst="255.255.255.255",
                         yiaddr="192.168.1.127", chaddr=addr1,
                         truncated_opt=True)
    if "OK" not in hapd.request("DATA_TEST_FRAME ifname=ap-br0 " + binascii.hexlify(pkt)):
        raise Exception("DATA_TEST_FRAME failed")

    # Wrong magic
    pkt = build_dhcp_ack(dst_ll=addr1, src_ll=bssid,
                         ip_src="192.168.1.1", ip_dst="255.255.255.255",
                         yiaddr="192.168.1.128", chaddr=addr1,
                         wrong_magic=True)
    if "OK" not in hapd.request("DATA_TEST_FRAME ifname=ap-br0 " + binascii.hexlify(pkt)):
        raise Exception("DATA_TEST_FRAME failed")

    # Wrong IPv4 total length
    pkt = build_dhcp_ack(dst_ll=addr1, src_ll=bssid,
                         ip_src="192.168.1.1", ip_dst="255.255.255.255",
                         yiaddr="192.168.1.129", chaddr=addr1,
                         force_tot_len=1000)
    if "OK" not in hapd.request("DATA_TEST_FRAME ifname=ap-br0 " + binascii.hexlify(pkt)):
        raise Exception("DATA_TEST_FRAME failed")

    # BOOTP
    pkt = build_dhcp_ack(dst_ll=addr1, src_ll=bssid,
                         ip_src="192.168.1.1", ip_dst="255.255.255.255",
                         yiaddr="192.168.1.129", chaddr=addr1,
                         no_dhcp=True)
    if "OK" not in hapd.request("DATA_TEST_FRAME ifname=ap-br0 " + binascii.hexlify(pkt)):
        raise Exception("DATA_TEST_FRAME failed")

    matches = get_permanent_neighbors("ap-br0")
    logger.info("After connect: " + str(matches))
    if len(matches) != 4:
        raise Exception("Unexpected number of neighbor entries after connect")
    if 'aaaa:bbbb:cccc::2 dev ap-br0 lladdr 02:00:00:00:00:00 PERMANENT' not in matches:
        raise Exception("dev0 addr missing")
    if 'aaaa:bbbb:dddd::2 dev ap-br0 lladdr 02:00:00:00:01:00 PERMANENT' not in matches:
        raise Exception("dev1 addr(1) missing")
    if 'aaaa:bbbb:eeee::2 dev ap-br0 lladdr 02:00:00:00:01:00 PERMANENT' not in matches:
        raise Exception("dev1 addr(2) missing")
    if '192.168.1.123 dev ap-br0 lladdr 02:00:00:00:00:00 PERMANENT' not in matches:
        raise Exception("dev0 IPv4 addr missing")

    targets = [ "192.168.1.123", "192.168.1.124", "192.168.1.125",
                "192.168.1.126" ]
    for target in targets:
        send_arp(dev[1], sender_ip="192.168.1.100", target_ip=target)

    for target in targets:
        send_arp(hapd, hapd_bssid=bssid, sender_ip="192.168.1.101",
                 target_ip=target)

    # ARP Probe from wireless STA
    send_arp(dev[1], target_ip="192.168.1.127")
    # ARP Announcement from wireless STA
    send_arp(dev[1], sender_ip="192.168.1.127", target_ip="192.168.1.127")
    send_arp(dev[1], sender_ip="192.168.1.127", target_ip="192.168.1.127",
             opcode=2)

    matches = get_permanent_neighbors("ap-br0")
    logger.info("After ARP Probe + Announcement: " + str(matches))

    # ARP Request for the newly introduced IP address from wireless STA
    send_arp(dev[0], sender_ip="192.168.1.123", target_ip="192.168.1.127")

    # ARP Request for the newly introduced IP address from bridge
    send_arp(hapd, hapd_bssid=bssid, sender_ip="192.168.1.102",
             target_ip="192.168.1.127")

    # ARP Probe from bridge
    send_arp(hapd, hapd_bssid=bssid, target_ip="192.168.1.130")
    # ARP Announcement from bridge (not to be learned by AP for proxyarp)
    send_arp(hapd, hapd_bssid=bssid, sender_ip="192.168.1.130",
             target_ip="192.168.1.130")
    send_arp(hapd, hapd_bssid=bssid, sender_ip="192.168.1.130",
             target_ip="192.168.1.130", opcode=2)

    matches = get_permanent_neighbors("ap-br0")
    logger.info("After ARP Probe + Announcement: " + str(matches))

    # ARP Request for the newly introduced IP address from wireless STA
    send_arp(dev[0], sender_ip="192.168.1.123", target_ip="192.168.1.130")
    # ARP Response from bridge (AP does not proxy for non-wireless devices)
    send_arp(hapd, hapd_bssid=bssid, dst_ll=addr0, sender_ip="192.168.1.130",
             target_ip="192.168.1.123", opcode=2)

    # ARP Request for the newly introduced IP address from bridge
    send_arp(hapd, hapd_bssid=bssid, sender_ip="192.168.1.102",
             target_ip="192.168.1.130")

    # ARP Probe from wireless STA (duplicate address; learned through DHCP)
    send_arp(dev[1], target_ip="192.168.1.123")
    # ARP Probe from wireless STA (duplicate address; learned through ARP)
    send_arp(dev[0], target_ip="192.168.1.127")

    # Gratuitous ARP Reply for another STA's IP address
    send_arp(dev[0], opcode=2, sender_mac=addr0, sender_ip="192.168.1.127",
             target_mac=addr1, target_ip="192.168.1.127")
    send_arp(dev[1], opcode=2, sender_mac=addr1, sender_ip="192.168.1.123",
             target_mac=addr0, target_ip="192.168.1.123")
    # ARP Request to verify previous mapping
    send_arp(dev[1], sender_ip="192.168.1.127", target_ip="192.168.1.123")
    send_arp(dev[0], sender_ip="192.168.1.123", target_ip="192.168.1.127")

    time.sleep(0.1)

    send_ns(dev[0], target="aaaa:bbbb:dddd::2", ip_src="aaaa:bbbb:cccc::2")
    time.sleep(0.1)
    send_ns(dev[1], target="aaaa:bbbb:cccc::2", ip_src="aaaa:bbbb:dddd::2")
    time.sleep(0.1)
    send_ns(hapd, hapd_bssid=bssid, target="aaaa:bbbb:dddd::2",
            ip_src="aaaa:bbbb:ffff::2")
    time.sleep(0.1)

    # Try to probe for an already assigned address
    send_ns(dev[1], target="aaaa:bbbb:cccc::2", ip_src="::")
    time.sleep(0.1)
    send_ns(hapd, hapd_bssid=bssid, target="aaaa:bbbb:cccc::2", ip_src="::")
    time.sleep(0.1)

    # Unsolicited NA
    send_na(dev[1], target="aaaa:bbbb:cccc:aeae::3",
            ip_src="aaaa:bbbb:cccc:aeae::3", ip_dst="ff02::1")
    send_na(hapd, hapd_bssid=bssid, target="aaaa:bbbb:cccc:aeae::4",
            ip_src="aaaa:bbbb:cccc:aeae::4", ip_dst="ff02::1")

    try:
        hwsim_utils.test_connectivity_iface(dev[0], hapd, "ap-br0")
    except Exception, e:
        logger.info("test_connectibity_iface failed: " + str(e))
        raise HwsimSkip("Assume kernel did not have the required patches for proxyarp")
    hwsim_utils.test_connectivity_iface(dev[1], hapd, "ap-br0")
    hwsim_utils.test_connectivity(dev[0], dev[1])

    dev[0].request("DISCONNECT")
    dev[1].request("DISCONNECT")
    time.sleep(0.5)
    for i in range(3):
        cmd[i].terminate()
    matches = get_permanent_neighbors("ap-br0")
    logger.info("After disconnect: " + str(matches))
    if len(matches) > 0:
        raise Exception("Unexpected neighbor entries after disconnect")
    cmd = subprocess.Popen(['ebtables', '-L', '--Lc'], stdout=subprocess.PIPE)
    res = cmd.stdout.read()
    cmd.stdout.close()
    logger.info("ebtables results:\n" + res)

def test_proxyarp_open(dev, apdev, params):
    """ProxyARP with open network"""
    try:
        _test_proxyarp_open(dev, apdev, params)
    finally:
        try:
            subprocess.call(['ebtables', '-F', 'FORWARD'])
            subprocess.call(['ebtables', '-F', 'OUTPUT'])
        except:
            pass
        subprocess.call(['ip', 'link', 'set', 'dev', 'ap-br0', 'down'],
                        stderr=open('/dev/null', 'w'))
        subprocess.call(['brctl', 'delbr', 'ap-br0'],
                        stderr=open('/dev/null', 'w'))

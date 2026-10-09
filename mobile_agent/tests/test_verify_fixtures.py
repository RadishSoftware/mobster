"""Accessibility trees captured from a fresh iPhone 17 Pro simulator (iOS 26.4, 28 Sep 2026) with the evidence
read (GET /source?format=xml&excluded_attributes=accessible,index), for the verify tests.

Settings' trees are Apple's app on a new simulator; the serial number, the host disk sizes and the process id
are replaced with placeholders. The SwiftUI trees come from a throwaway fixture app with fictional content
(dev.mobster.tabsfixture) and from Daybreak, the sample app in examples/ios/Daybreak (dev.mobster.daybreak, process
id replaced). The tests at the bottom keep them free of anything identifying."""

import re
import unittest

# Settings' first screen
SETTINGS_ROOT = r'''<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="Settings" label="Settings" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="" processId="1000" bundleId="com.apple.Preferences">
  <XCUIElementTypeWindow type="XCUIElementTypeWindow" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
      <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
        <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                    <XCUIElementTypeNavigationBar type="XCUIElementTypeNavigationBar" name="Settings" enabled="true" visible="true" x="0" y="62" width="402" height="106" traits="">
                      <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Settings" name="Settings" label="Settings" enabled="true" visible="true" x="16" y="119" width="133" height="42" traits="Header"/>
                    </XCUIElementTypeNavigationBar>
                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                      <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                        <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                            <XCUIElementTypeCollectionView type="XCUIElementTypeCollectionView" enabled="true" visible="false" x="0" y="0" width="402" height="874" traits="">
                              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="-16" y="-1580" width="434" height="1856" traits=""/>
                              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits=""/>
                              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="733" width="402" height="141" traits="">
                                  <XCUIElementTypeImage type="XCUIElementTypeImage" name="AdditionalDimmingOverlay" enabled="true" visible="true" x="-120" y="738" width="642" height="272" traits="Image"/>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="733" width="402" height="141" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="733" width="402" height="141" traits=""/>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="733" width="402" height="141" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="733" width="402" height="141" traits=""/>
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="733" width="402" height="141" traits=""/>
                                  </XCUIElementTypeOther>
                                </XCUIElementTypeOther>
                              </XCUIElementTypeOther>
                              <XCUIElementTypeCell type="XCUIElementTypeCell" enabled="true" visible="true" x="16" y="168" width="370" height="91" traits="">
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="168" width="370" height="91" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="168" width="370" height="91" traits=""/>
                                </XCUIElementTypeOther>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="168" width="370" height="91" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="168" width="370" height="91" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="168" width="370" height="91" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="com.apple.settings.primaryAppleAccount" label="Apple Account, Sign in to access your iCloud data, the App Store, Apple services, and more." enabled="true" visible="true" x="16" y="168" width="370" height="91" traits="StaticText, Button">
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" name="apple.id" label="apple.id" enabled="true" visible="true" x="32" y="183" width="60" height="61" traits="Image"/>
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Apple Account" name="Apple Account" label="Apple Account" enabled="true" visible="true" x="100" y="183" width="135" height="24" traits="StaticText"/>
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Sign in to access your iCloud data, the App Store, Apple services, and more." name="Sign in to access your iCloud data, the App Store, Apple services, and more." label="Sign in to access your iCloud data, the App Store, Apple services, and more." enabled="true" visible="true" x="100" y="209" width="236" height="35" traits="StaticText"/>
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" name="chevron.forward" enabled="true" visible="true" x="360" y="207" width="8" height="12" traits="Image"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                </XCUIElementTypeOther>
                              </XCUIElementTypeCell>
                              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="-16" y="276" width="434" height="87" traits=""/>
                              <XCUIElementTypeCell type="XCUIElementTypeCell" enabled="true" visible="true" x="16" y="293" width="370" height="53" traits="">
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="293" width="370" height="53" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="293" width="370" height="53" traits=""/>
                                </XCUIElementTypeOther>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="293" width="370" height="53" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="293" width="370" height="53" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="293" width="370" height="53" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="com.apple.settings.followUpItem.com.apple.icloud.gm" label="Ready for Apple Intelligence" enabled="true" visible="true" x="16" y="293" width="370" height="53" traits="StaticText, Button">
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Ready for Apple Intelligence" name="Ready for Apple Intelligence" label="Ready for Apple Intelligence" enabled="true" visible="true" x="32" y="309" width="213" height="21" traits="StaticText"/>
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" name="chevron.forward" enabled="true" visible="true" x="360" y="313" width="8" height="13" traits="Image"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                </XCUIElementTypeOther>
                              </XCUIElementTypeCell>
                              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="-16" y="363" width="434" height="451" traits=""/>
                              <XCUIElementTypeCell type="XCUIElementTypeCell" enabled="true" visible="true" x="16" y="380" width="370" height="53" traits="">
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="380" width="370" height="53" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="380" width="370" height="53" traits=""/>
                                </XCUIElementTypeOther>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="380" width="370" height="53" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="380" width="370" height="53" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="380" width="370" height="53" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="com.apple.settings.general" label="General" enabled="true" visible="true" x="16" y="380" width="370" height="53" traits="StaticText, Button">
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" enabled="true" visible="true" x="29" y="392" width="30" height="30" traits="Image"/>
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="General" name="General" label="General" enabled="true" visible="true" x="29" y="392" width="103" height="29" traits="StaticText"/>
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" name="chevron.forward" enabled="true" visible="true" x="360" y="400" width="8" height="13" traits="Image"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                </XCUIElementTypeOther>
                              </XCUIElementTypeCell>
                              <XCUIElementTypeCell type="XCUIElementTypeCell" enabled="true" visible="true" x="16" y="432" width="370" height="53" traits="">
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="432" width="370" height="53" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="432" width="370" height="53" traits=""/>
                                </XCUIElementTypeOther>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="432" width="370" height="53" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="432" width="370" height="53" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="432" width="370" height="53" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="com.apple.settings.accessibility" label="Accessibility" enabled="true" visible="true" x="16" y="432" width="370" height="53" traits="StaticText, Button">
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" enabled="true" visible="true" x="29" y="444" width="30" height="29" traits="Image"/>
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Accessibility" name="Accessibility" label="Accessibility" enabled="true" visible="true" x="29" y="444" width="138" height="29" traits="StaticText"/>
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" name="chevron.forward" enabled="true" visible="true" x="360" y="452" width="8" height="13" traits="Image"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                </XCUIElementTypeOther>
                              </XCUIElementTypeCell>
                              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="72" y="431" width="298" height="2" traits=""/>
                              <XCUIElementTypeCell type="XCUIElementTypeCell" enabled="true" visible="true" x="16" y="484" width="370" height="53" traits="">
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="484" width="370" height="53" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="484" width="370" height="53" traits=""/>
                                </XCUIElementTypeOther>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="484" width="370" height="53" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="484" width="370" height="53" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="484" width="370" height="53" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="com.apple.settings.actionButton" label="Action Button" enabled="true" visible="true" x="16" y="484" width="370" height="53" traits="StaticText, Button">
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" enabled="true" visible="true" x="29" y="496" width="30" height="29" traits="Image"/>
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Action Button" name="Action Button" label="Action Button" enabled="true" visible="true" x="29" y="496" width="147" height="29" traits="StaticText"/>
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" name="chevron.forward" enabled="true" visible="true" x="360" y="504" width="8" height="13" traits="Image"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                </XCUIElementTypeOther>
                              </XCUIElementTypeCell>
                              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="72" y="483" width="298" height="2" traits=""/>
                              <XCUIElementTypeCell type="XCUIElementTypeCell" enabled="true" visible="true" x="16" y="536" width="370" height="53" traits="">
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="536" width="370" height="53" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="536" width="370" height="53" traits=""/>
                                </XCUIElementTypeOther>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="536" width="370" height="53" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="536" width="370" height="53" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="536" width="370" height="53" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="com.apple.settings.siri" label="Apple Intelligence &amp; Siri" enabled="true" visible="true" x="16" y="536" width="370" height="53" traits="StaticText, Button">
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" enabled="true" visible="true" x="29" y="547" width="30" height="30" traits="Image"/>
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Apple Intelligence &amp; Siri" name="Apple Intelligence &amp; Siri" label="Apple Intelligence &amp; Siri" enabled="true" visible="true" x="29" y="548" width="223" height="29" traits="StaticText"/>
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" name="chevron.forward" enabled="true" visible="true" x="360" y="556" width="8" height="13" traits="Image"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                </XCUIElementTypeOther>
                              </XCUIElementTypeCell>
                              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="72" y="535" width="298" height="2" traits=""/>
                              <XCUIElementTypeCell type="XCUIElementTypeCell" enabled="true" visible="true" x="16" y="588" width="370" height="53" traits="">
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="588" width="370" height="53" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="588" width="370" height="53" traits=""/>
                                </XCUIElementTypeOther>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="588" width="370" height="53" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="588" width="370" height="53" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="588" width="370" height="53" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="com.apple.settings.camera" label="Camera" enabled="true" visible="true" x="16" y="588" width="370" height="53" traits="StaticText, Button">
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" enabled="true" visible="true" x="29" y="599" width="30" height="30" traits="Image"/>
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Camera" name="Camera" label="Camera" enabled="true" visible="true" x="29" y="600" width="103" height="29" traits="StaticText"/>
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" name="chevron.forward" enabled="true" visible="true" x="360" y="608" width="8" height="13" traits="Image"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                </XCUIElementTypeOther>
                              </XCUIElementTypeCell>
                              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="72" y="587" width="298" height="2" traits=""/>
                              <XCUIElementTypeCell type="XCUIElementTypeCell" enabled="true" visible="true" x="16" y="640" width="370" height="53" traits="">
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="640" width="370" height="53" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="640" width="370" height="53" traits=""/>
                                </XCUIElementTypeOther>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="640" width="370" height="53" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="640" width="370" height="53" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="640" width="370" height="53" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="com.apple.settings.homeScreen" label="Home Screen &amp; App Library" enabled="true" visible="true" x="16" y="640" width="370" height="53" traits="StaticText, Button">
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" enabled="true" visible="true" x="29" y="651" width="30" height="30" traits="Image"/>
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Home Screen &amp; App Library" name="Home Screen &amp; App Library" label="Home Screen &amp; App Library" enabled="true" visible="true" x="29" y="652" width="255" height="29" traits="StaticText"/>
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" name="chevron.forward" enabled="true" visible="true" x="360" y="660" width="8" height="13" traits="Image"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                </XCUIElementTypeOther>
                              </XCUIElementTypeCell>
                              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="72" y="639" width="298" height="2" traits=""/>
                              <XCUIElementTypeCell type="XCUIElementTypeCell" enabled="true" visible="true" x="16" y="692" width="370" height="53" traits="">
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="692" width="370" height="53" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="692" width="370" height="53" traits=""/>
                                </XCUIElementTypeOther>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="692" width="370" height="53" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="692" width="370" height="53" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="692" width="370" height="53" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="com.apple.settings.search" label="Search" enabled="true" visible="true" x="16" y="692" width="370" height="53" traits="StaticText, Button">
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" enabled="true" visible="true" x="29" y="704" width="30" height="29" traits="Image"/>
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Search" name="Search" label="Search" enabled="true" visible="true" x="29" y="704" width="97" height="29" traits="StaticText"/>
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" name="chevron.forward" enabled="true" visible="true" x="360" y="712" width="8" height="13" traits="Image"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                </XCUIElementTypeOther>
                              </XCUIElementTypeCell>
                              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="72" y="691" width="298" height="2" traits=""/>
                              <XCUIElementTypeCell type="XCUIElementTypeCell" enabled="true" visible="true" x="16" y="744" width="370" height="53" traits="">
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="744" width="370" height="53" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="744" width="370" height="53" traits=""/>
                                </XCUIElementTypeOther>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="744" width="370" height="53" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="744" width="370" height="53" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="744" width="370" height="53" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="com.apple.settings.standBy" label="StandBy" enabled="true" visible="true" x="16" y="744" width="370" height="53" traits="StaticText, Button">
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" enabled="true" visible="true" x="29" y="756" width="30" height="29" traits="Image"/>
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="StandBy" name="StandBy" label="StandBy" enabled="true" visible="true" x="29" y="756" width="108" height="29" traits="StaticText"/>
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" name="chevron.forward" enabled="true" visible="true" x="360" y="764" width="8" height="13" traits="Image"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                </XCUIElementTypeOther>
                              </XCUIElementTypeCell>
                              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="72" y="743" width="298" height="2" traits=""/>
                              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="-16" y="813" width="434" height="88" traits=""/>
                              <XCUIElementTypeCell type="XCUIElementTypeCell" enabled="true" visible="false" x="16" y="831" width="370" height="53" traits="">
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="831" width="370" height="53" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="831" width="370" height="53" traits=""/>
                                </XCUIElementTypeOther>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="831" width="370" height="53" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="831" width="370" height="53" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="831" width="370" height="53" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="com.apple.settings.screenTime" label="Screen Time" enabled="true" visible="false" x="16" y="831" width="370" height="53" traits="StaticText, Button">
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" enabled="true" visible="false" x="29" y="843" width="30" height="29" traits="Image"/>
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Screen Time" name="Screen Time" label="Screen Time" enabled="true" visible="false" x="29" y="843" width="139" height="29" traits="StaticText"/>
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" name="chevron.forward" enabled="true" visible="false" x="360" y="851" width="8" height="13" traits="Image"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                </XCUIElementTypeOther>
                              </XCUIElementTypeCell>
                              <XCUIElementTypeOther type="XCUIElementTypeOther" value="0%" name="Vertical scroll bar, 2 pages" label="Vertical scroll bar, 2 pages" enabled="true" visible="true" x="369" y="116" width="30" height="672" traits="Adjustable">
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="396" y="119" width="3" height="430" traits=""/>
                              </XCUIElementTypeOther>
                              <XCUIElementTypeOther type="XCUIElementTypeOther" value="0%" name="Vertical scroll bar, 2 pages" label="Vertical scroll bar, 2 pages" enabled="true" visible="true" x="369" y="116" width="30" height="672" traits="Adjustable">
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="396" y="119" width="3" height="430" traits=""/>
                              </XCUIElementTypeOther>
                            </XCUIElementTypeCollectionView>
                            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="201" y="478" width="0" height="0" traits=""/>
                          </XCUIElementTypeOther>
                        </XCUIElementTypeOther>
                      </XCUIElementTypeOther>
                    </XCUIElementTypeOther>
                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                      <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                        <XCUIElementTypeToolbar type="XCUIElementTypeToolbar" name="Toolbar" label="Toolbar" enabled="true" visible="true" x="0" y="788" width="402" height="86" traits="">
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="28" y="798" width="346" height="48" traits="">
                            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="28" y="798" width="346" height="48" traits="">
                              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="28" y="798" width="0" height="0" traits="">
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="27" y="798" width="347" height="48" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="27" y="798" width="347" height="48" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="32" y="803" width="337" height="38" traits="">
                                      <XCUIElementTypeSearchField type="XCUIElementTypeSearchField" value="Search" name="Search" label="Search" enabled="true" visible="true" x="32" y="803" width="337" height="38" placeholderValue="Search" traits="SearchField">
                                        <XCUIElementTypeButton type="XCUIElementTypeButton" name="Dictate" label="Dictate" enabled="true" visible="true" x="335" y="811" width="18" height="22" traits="Button"/>
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" name="magnifyingglass" label="Search" enabled="true" visible="true" x="45" y="812" width="22" height="19" traits="Image"/>
                                        <XCUIElementTypeButton type="XCUIElementTypeButton" name="Dictate" label="Dictate" enabled="true" visible="true" x="335" y="811" width="18" height="22" traits="Button"/>
                                      </XCUIElementTypeSearchField>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                </XCUIElementTypeOther>
                              </XCUIElementTypeOther>
                            </XCUIElementTypeOther>
                          </XCUIElementTypeOther>
                        </XCUIElementTypeToolbar>
                      </XCUIElementTypeOther>
                    </XCUIElementTypeOther>
                  </XCUIElementTypeOther>
                </XCUIElementTypeOther>
              </XCUIElementTypeOther>
              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits=""/>
              </XCUIElementTypeOther>
            </XCUIElementTypeOther>
          </XCUIElementTypeOther>
        </XCUIElementTypeOther>
      </XCUIElementTypeOther>
    </XCUIElementTypeOther>
  </XCUIElementTypeWindow>
</XCUIElementTypeApplication>
'''

# Settings > General
SETTINGS_GENERAL = r'''<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="Settings" label="Settings" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="" processId="1000" bundleId="com.apple.Preferences">
  <XCUIElementTypeWindow type="XCUIElementTypeWindow" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
      <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
        <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                    <XCUIElementTypeNavigationBar type="XCUIElementTypeNavigationBar" name="General" enabled="true" visible="true" x="0" y="62" width="402" height="54" traits="">
                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="BackButton" label="Settings" enabled="true" visible="true" x="16" y="62" width="44" height="44" traits="Button"/>
                      <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="General" name="General" label="General" enabled="true" visible="true" x="170" y="73" width="62" height="22" traits="Header"/>
                    </XCUIElementTypeNavigationBar>
                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                      <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                        <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="116" width="402" height="724" traits="">
                              <XCUIElementTypeTable type="XCUIElementTypeTable" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="116" width="402" height="18" traits=""/>
                                <XCUIElementTypeCell type="XCUIElementTypeCell" name="PLACARD" enabled="true" visible="true" x="20" y="133" width="362" height="224" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="133" width="362" height="224" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="133" width="362" height="224" traits="">
                                      <XCUIElementTypeImage type="XCUIElementTypeImage" enabled="true" visible="false" x="36" y="149" width="60" height="61" traits="Image"/>
                                      <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="General" name="General" label="General" enabled="true" visible="true" x="36" y="224" width="80" height="27" traits="Header, StaticText"/>
                                      <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Manage your overall setup and preferences for iPhone, such as software updates, device language, CarPlay, AirDrop, and more." name="Manage your overall setup and preferences for iPhone, such as software updates, device language, CarPlay, AirDrop, and more." label="Manage your overall setup and preferences for iPhone, such as software updates, device language, CarPlay, AirDrop, and more." enabled="true" visible="true" x="36" y="254" width="330" height="87" traits="Header, StaticText"/>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="133" width="362" height="224" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="133" width="362" height="224" traits=""/>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="20" y="355" width="362" height="2" traits=""/>
                                </XCUIElementTypeCell>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="356" width="402" height="18" traits=""/>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="373" width="402" height="19" traits=""/>
                                <XCUIElementTypeCell type="XCUIElementTypeCell" name="About" label="About" enabled="true" visible="true" x="20" y="391" width="362" height="54" traits="Button">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="391" width="332" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="391" width="332" height="54" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="About" label="About" enabled="true" visible="true" x="34" y="404" width="88" height="28" traits="StaticText, Button">
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="About" name="About" label="About" enabled="true" visible="true" x="34" y="404" width="88" height="28" traits="StaticText"/>
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" enabled="true" visible="true" x="34" y="403" width="28" height="29" traits="Image"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="391" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="391" width="362" height="54" traits=""/>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="76" y="443" width="290" height="2" traits=""/>
                                  <XCUIElementTypeButton type="XCUIElementTypeButton" name="chevron" label="chevron" enabled="false" visible="true" x="351" y="410" width="11" height="15" traits="NotEnabled, Button"/>
                                </XCUIElementTypeCell>
                                <XCUIElementTypeCell type="XCUIElementTypeCell" name="SCREEN_CAPTURE" label="Screen Capture" enabled="true" visible="true" x="20" y="444" width="362" height="54" traits="Button">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="444" width="332" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="444" width="332" height="54" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="SCREEN_CAPTURE" label="Screen Capture" enabled="true" visible="true" x="34" y="457" width="161" height="28" traits="StaticText, Button">
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Screen Capture" name="Screen Capture" label="Screen Capture" enabled="true" visible="true" x="34" y="457" width="161" height="28" traits="StaticText"/>
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" enabled="true" visible="true" x="34" y="456" width="28" height="29" traits="Image"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="444" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="444" width="362" height="54" traits=""/>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="20" y="496" width="362" height="2" traits=""/>
                                  <XCUIElementTypeButton type="XCUIElementTypeButton" name="chevron" label="chevron" enabled="false" visible="true" x="351" y="463" width="11" height="15" traits="NotEnabled, Button"/>
                                </XCUIElementTypeCell>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="497" width="402" height="18" traits=""/>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="514" width="402" height="19" traits=""/>
                                <XCUIElementTypeCell type="XCUIElementTypeCell" name="AUTOFILL" label="AutoFill &amp; Passwords" enabled="true" visible="true" x="20" y="532" width="362" height="54" traits="Button">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="532" width="332" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="532" width="332" height="54" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="AUTOFILL" label="AutoFill &amp; Passwords" enabled="true" visible="true" x="34" y="545" width="201" height="28" traits="StaticText, Button">
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="AutoFill &amp; Passwords" name="AutoFill &amp; Passwords" label="AutoFill &amp; Passwords" enabled="true" visible="true" x="34" y="545" width="201" height="28" traits="StaticText"/>
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" enabled="true" visible="true" x="34" y="544" width="28" height="29" traits="Image"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="532" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="532" width="362" height="54" traits=""/>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="76" y="584" width="290" height="2" traits=""/>
                                  <XCUIElementTypeButton type="XCUIElementTypeButton" name="chevron" label="chevron" enabled="false" visible="true" x="351" y="551" width="11" height="15" traits="NotEnabled, Button"/>
                                </XCUIElementTypeCell>
                                <XCUIElementTypeCell type="XCUIElementTypeCell" name="DICTIONARY" label="Dictionary" enabled="true" visible="true" x="20" y="585" width="362" height="54" traits="Button">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="585" width="332" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="585" width="332" height="54" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="DICTIONARY" label="Dictionary" enabled="true" visible="true" x="34" y="598" width="120" height="28" traits="StaticText, Button">
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Dictionary" name="Dictionary" label="Dictionary" enabled="true" visible="true" x="34" y="598" width="120" height="28" traits="StaticText"/>
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" enabled="true" visible="true" x="34" y="597" width="28" height="29" traits="Image"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="585" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="585" width="362" height="54" traits=""/>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="76" y="637" width="290" height="2" traits=""/>
                                  <XCUIElementTypeButton type="XCUIElementTypeButton" name="chevron" label="chevron" enabled="false" visible="true" x="351" y="604" width="11" height="15" traits="NotEnabled, Button"/>
                                </XCUIElementTypeCell>
                                <XCUIElementTypeCell type="XCUIElementTypeCell" name="FONT_SETTING" label="Fonts" enabled="true" visible="true" x="20" y="638" width="362" height="54" traits="Button">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="638" width="332" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="638" width="332" height="54" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="FONT_SETTING" label="Fonts" enabled="true" visible="true" x="34" y="651" width="85" height="28" traits="StaticText, Button">
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Fonts" name="Fonts" label="Fonts" enabled="true" visible="true" x="34" y="651" width="85" height="28" traits="StaticText"/>
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" enabled="true" visible="true" x="34" y="650" width="28" height="29" traits="Image"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="638" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="638" width="362" height="54" traits=""/>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="76" y="690" width="290" height="2" traits=""/>
                                  <XCUIElementTypeButton type="XCUIElementTypeButton" name="chevron" label="chevron" enabled="false" visible="true" x="351" y="657" width="11" height="15" traits="NotEnabled, Button"/>
                                </XCUIElementTypeCell>
                                <XCUIElementTypeCell type="XCUIElementTypeCell" name="Keyboard" label="Keyboard" enabled="true" visible="true" x="20" y="691" width="362" height="54" traits="Button">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="691" width="332" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="691" width="332" height="54" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="Keyboard" label="Keyboard" enabled="true" visible="true" x="34" y="704" width="115" height="28" traits="StaticText, Button">
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Keyboard" name="Keyboard" label="Keyboard" enabled="true" visible="true" x="34" y="704" width="115" height="28" traits="StaticText"/>
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" enabled="true" visible="true" x="34" y="703" width="28" height="29" traits="Image"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="691" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="691" width="362" height="54" traits=""/>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="76" y="743" width="290" height="2" traits=""/>
                                  <XCUIElementTypeButton type="XCUIElementTypeButton" name="chevron" label="chevron" enabled="false" visible="true" x="351" y="710" width="11" height="15" traits="NotEnabled, Button"/>
                                </XCUIElementTypeCell>
                                <XCUIElementTypeCell type="XCUIElementTypeCell" name="INTERNATIONAL" label="Language &amp; Region" enabled="true" visible="true" x="20" y="744" width="362" height="54" traits="Button">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="744" width="332" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="744" width="332" height="54" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="INTERNATIONAL" label="Language &amp; Region" enabled="true" visible="true" x="34" y="757" width="191" height="28" traits="StaticText, Button">
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Language &amp; Region" name="Language &amp; Region" label="Language &amp; Region" enabled="true" visible="true" x="34" y="757" width="191" height="28" traits="StaticText"/>
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" enabled="true" visible="true" x="34" y="756" width="28" height="29" traits="Image"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="744" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="744" width="362" height="54" traits=""/>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="20" y="796" width="362" height="2" traits=""/>
                                  <XCUIElementTypeButton type="XCUIElementTypeButton" name="chevron" label="chevron" enabled="false" visible="true" x="351" y="763" width="11" height="15" traits="NotEnabled, Button"/>
                                </XCUIElementTypeCell>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="797" width="402" height="18" traits=""/>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="814" width="402" height="19" traits=""/>
                                <XCUIElementTypeCell type="XCUIElementTypeCell" name="ManagedConfigurationList" label="VPN &amp; Device Management" enabled="true" visible="true" x="20" y="832" width="362" height="54" traits="Button">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="832" width="332" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="832" width="332" height="54" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="ManagedConfigurationList" label="VPN &amp; Device Management" enabled="true" visible="false" x="34" y="845" width="253" height="28" traits="StaticText, Button">
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="VPN &amp; Device Management" name="VPN &amp; Device Management" label="VPN &amp; Device Management" enabled="true" visible="false" x="34" y="845" width="253" height="28" traits="StaticText"/>
                                        <XCUIElementTypeImage type="XCUIElementTypeImage" enabled="true" visible="false" x="34" y="844" width="28" height="29" traits="Image"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="832" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="832" width="362" height="54" traits=""/>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="20" y="884" width="362" height="2" traits=""/>
                                  <XCUIElementTypeButton type="XCUIElementTypeButton" name="chevron" label="chevron" enabled="false" visible="false" x="351" y="851" width="11" height="15" traits="NotEnabled, Button"/>
                                </XCUIElementTypeCell>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="832" width="402" height="18" traits=""/>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="0" y="902" width="402" height="19" traits=""/>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="0" y="920" width="402" height="18" traits=""/>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" value="0%" name="Vertical scroll bar, 2 pages" label="Vertical scroll bar, 2 pages" enabled="true" visible="false" x="369" y="116" width="30" height="696" traits="Adjustable">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="396" y="119" width="3" height="608" traits=""/>
                                </XCUIElementTypeOther>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" value="0%" name="Horizontal scroll bar, 1 page" label="Horizontal scroll bar, 1 page" enabled="true" visible="false" x="62" y="841" width="278" height="30" traits="Adjustable">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="65" y="868" width="272" height="3" traits=""/>
                                </XCUIElementTypeOther>
                              </XCUIElementTypeTable>
                            </XCUIElementTypeOther>
                          </XCUIElementTypeOther>
                        </XCUIElementTypeOther>
                      </XCUIElementTypeOther>
                    </XCUIElementTypeOther>
                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="0" y="0" width="402" height="874" traits="">
                      <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="0" y="0" width="402" height="874" traits=""/>
                    </XCUIElementTypeOther>
                  </XCUIElementTypeOther>
                </XCUIElementTypeOther>
              </XCUIElementTypeOther>
              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits=""/>
              </XCUIElementTypeOther>
            </XCUIElementTypeOther>
          </XCUIElementTypeOther>
        </XCUIElementTypeOther>
      </XCUIElementTypeOther>
    </XCUIElementTypeOther>
  </XCUIElementTypeWindow>
</XCUIElementTypeApplication>
'''

# Settings > General > About
SETTINGS_ABOUT = r'''<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="Settings" label="Settings" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="" processId="1000" bundleId="com.apple.Preferences">
  <XCUIElementTypeWindow type="XCUIElementTypeWindow" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
      <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
        <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                    <XCUIElementTypeNavigationBar type="XCUIElementTypeNavigationBar" name="About" enabled="true" visible="true" x="0" y="62" width="402" height="54" traits="">
                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="BackButton" label="General" enabled="true" visible="true" x="16" y="62" width="44" height="44" traits="Button"/>
                      <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="About" name="About" label="About" enabled="true" visible="true" x="176" y="73" width="49" height="22" traits="Header"/>
                    </XCUIElementTypeNavigationBar>
                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                      <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                        <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="116" width="402" height="724" traits="">
                              <XCUIElementTypeTable type="XCUIElementTypeTable" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="116" width="402" height="18" traits=""/>
                                <XCUIElementTypeCell type="XCUIElementTypeCell" name="NAME_CELL_ID" label="Name, iPhone" enabled="true" visible="true" x="20" y="133" width="362" height="54" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="133" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="133" width="362" height="54" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="NAME_CELL_ID" label="Name, iPhone" enabled="true" visible="true" x="36" y="150" width="330" height="21" traits="StaticText, Button">
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Name" name="Name" label="Name" enabled="true" visible="true" x="36" y="149" width="45" height="22" traits="StaticText"/>
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="iPhone" name="iPhone" label="iPhone" enabled="true" visible="true" x="313" y="149" width="53" height="22" traits="StaticText"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="133" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="133" width="362" height="54" traits=""/>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="36" y="185" width="330" height="2" traits=""/>
                                </XCUIElementTypeCell>
                                <XCUIElementTypeCell type="XCUIElementTypeCell" name="SW_VERSION_SPECIFIER" label="iOS Version, 26.4" enabled="true" visible="true" x="20" y="186" width="362" height="54" traits="Button">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="186" width="332" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="186" width="332" height="54" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="SW_VERSION_SPECIFIER" label="iOS Version, 26.4" enabled="true" visible="true" x="36" y="203" width="308" height="21" traits="StaticText, Button">
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="iOS Version" name="iOS Version" label="iOS Version" enabled="true" visible="true" x="36" y="202" width="88" height="22" traits="StaticText"/>
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="26.4" name="26.4" label="26.4" enabled="true" visible="true" x="309" y="202" width="35" height="22" traits="StaticText"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="186" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="186" width="362" height="54" traits=""/>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="36" y="238" width="330" height="2" traits=""/>
                                  <XCUIElementTypeButton type="XCUIElementTypeButton" name="chevron" label="chevron" enabled="false" visible="true" x="351" y="205" width="11" height="15" traits="NotEnabled, Button"/>
                                </XCUIElementTypeCell>
                                <XCUIElementTypeCell type="XCUIElementTypeCell" name="ProductModelName" label="Model Name, iPhone 17 Pro" enabled="true" visible="true" x="20" y="239" width="362" height="54" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="239" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="239" width="362" height="54" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="ProductModelName" label="Model Name, iPhone 17 Pro" enabled="true" visible="true" x="36" y="256" width="330" height="21" traits="StaticText, Button">
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Model Name" name="Model Name" label="Model Name" enabled="true" visible="true" x="36" y="255" width="97" height="22" traits="StaticText"/>
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="iPhone 17 Pro" name="iPhone 17 Pro" label="iPhone 17 Pro" enabled="true" visible="true" x="262" y="255" width="104" height="22" traits="StaticText"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="239" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="239" width="362" height="54" traits=""/>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="36" y="291" width="330" height="2" traits=""/>
                                </XCUIElementTypeCell>
                                <XCUIElementTypeCell type="XCUIElementTypeCell" name="ProductModel" label="Model Number, A3256LL/A" enabled="true" visible="true" x="20" y="292" width="362" height="54" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="292" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="292" width="362" height="54" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="ProductModel" label="Model Number, A3256LL/A" enabled="true" visible="true" x="36" y="309" width="330" height="21" traits="StaticText, Button">
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Model Number" name="Model Number" label="Model Number" enabled="true" visible="true" x="36" y="308" width="113" height="22" traits="StaticText"/>
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="A3256LL/A" name="A3256LL/A" label="A3256LL/A" enabled="true" visible="true" x="280" y="308" width="86" height="22" traits="StaticText"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="292" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="292" width="362" height="54" traits=""/>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="36" y="344" width="330" height="2" traits=""/>
                                </XCUIElementTypeCell>
                                <XCUIElementTypeCell type="XCUIElementTypeCell" name="SerialNumber" label="Serial Number, SIM0SERIAL0" enabled="true" visible="true" x="20" y="345" width="362" height="54" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="345" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="345" width="362" height="54" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="SerialNumber" label="Serial Number, SIM0SERIAL0" enabled="true" visible="true" x="36" y="362" width="330" height="21" traits="StaticText, Button">
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Serial Number" name="Serial Number" label="Serial Number" enabled="true" visible="true" x="36" y="361" width="109" height="22" traits="StaticText"/>
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="SIM0SERIAL0" name="SIM0SERIAL0" label="SIM0SERIAL0" enabled="true" visible="true" x="257" y="361" width="109" height="22" traits="StaticText"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="345" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="345" width="362" height="54" traits=""/>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="20" y="397" width="362" height="2" traits=""/>
                                </XCUIElementTypeCell>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="398" width="402" height="19" traits=""/>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="416" width="402" height="18" traits=""/>
                                <XCUIElementTypeCell type="XCUIElementTypeCell" name="SONGS" label="Songs, 0" enabled="true" visible="true" x="20" y="433" width="362" height="54" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="433" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="433" width="362" height="54" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="SONGS" label="Songs, 0" enabled="true" visible="true" x="36" y="450" width="330" height="21" traits="StaticText, Button">
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Songs" name="Songs" label="Songs" enabled="true" visible="true" x="36" y="449" width="48" height="22" traits="StaticText"/>
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="0" name="0" label="0" enabled="true" visible="true" x="355" y="449" width="11" height="22" traits="StaticText"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="433" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="433" width="362" height="54" traits=""/>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="36" y="485" width="330" height="2" traits=""/>
                                </XCUIElementTypeCell>
                                <XCUIElementTypeCell type="XCUIElementTypeCell" name="VIDEOS" label="Videos, 0" enabled="true" visible="true" x="20" y="486" width="362" height="54" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="486" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="486" width="362" height="54" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="VIDEOS" label="Videos, 0" enabled="true" visible="true" x="36" y="503" width="330" height="21" traits="StaticText, Button">
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Videos" name="Videos" label="Videos" enabled="true" visible="true" x="36" y="502" width="53" height="22" traits="StaticText"/>
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="0" name="0" label="0" enabled="true" visible="true" x="355" y="502" width="11" height="22" traits="StaticText"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="486" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="486" width="362" height="54" traits=""/>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="36" y="538" width="330" height="2" traits=""/>
                                </XCUIElementTypeCell>
                                <XCUIElementTypeCell type="XCUIElementTypeCell" name="PHOTOS" label="Photos, 6" enabled="true" visible="true" x="20" y="539" width="362" height="54" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="539" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="539" width="362" height="54" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="PHOTOS" label="Photos, 6" enabled="true" visible="true" x="36" y="556" width="330" height="21" traits="StaticText, Button">
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Photos" name="Photos" label="Photos" enabled="true" visible="true" x="36" y="555" width="54" height="22" traits="StaticText"/>
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="6" name="6" label="6" enabled="true" visible="true" x="355" y="555" width="11" height="22" traits="StaticText"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="539" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="539" width="362" height="54" traits=""/>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="36" y="591" width="330" height="2" traits=""/>
                                </XCUIElementTypeCell>
                                <XCUIElementTypeCell type="XCUIElementTypeCell" name="APPLICATIONS" label="Applications, 2" enabled="true" visible="true" x="20" y="592" width="362" height="54" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="592" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="592" width="362" height="54" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="APPLICATIONS" label="Applications, 2" enabled="true" visible="true" x="36" y="609" width="330" height="21" traits="StaticText, Button">
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Applications" name="Applications" label="Applications" enabled="true" visible="true" x="36" y="608" width="94" height="22" traits="StaticText"/>
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="2" name="2" label="2" enabled="true" visible="true" x="356" y="608" width="10" height="22" traits="StaticText"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="592" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="592" width="362" height="54" traits=""/>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="36" y="644" width="330" height="2" traits=""/>
                                </XCUIElementTypeCell>
                                <XCUIElementTypeCell type="XCUIElementTypeCell" name="User Data Capacity" label="Capacity, 256 GB" enabled="true" visible="true" x="20" y="645" width="362" height="54" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="645" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="645" width="362" height="54" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="User Data Capacity" label="Capacity, 256 GB" enabled="true" visible="true" x="36" y="662" width="330" height="21" traits="StaticText, Button">
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Capacity" name="Capacity" label="Capacity" enabled="true" visible="true" x="36" y="661" width="67" height="22" traits="StaticText"/>
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="256 GB" name="256 GB" label="256 GB" enabled="true" visible="true" x="330" y="661" width="36" height="22" traits="StaticText"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="645" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="645" width="362" height="54" traits=""/>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="36" y="697" width="330" height="2" traits=""/>
                                </XCUIElementTypeCell>
                                <XCUIElementTypeCell type="XCUIElementTypeCell" name="User Data Available" label="Available, 118.20 GB" enabled="true" visible="true" x="20" y="698" width="362" height="54" traits="">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="698" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="698" width="362" height="54" traits="">
                                      <XCUIElementTypeButton type="XCUIElementTypeButton" name="User Data Available" label="Available, 118.20 GB" enabled="true" visible="true" x="36" y="715" width="330" height="21" traits="StaticText, Button">
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Available" name="Available" label="Available" enabled="true" visible="true" x="36" y="714" width="68" height="22" traits="StaticText"/>
                                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="118.20 GB" name="118.20 GB" label="118.20 GB" enabled="true" visible="true" x="287" y="714" width="79" height="22" traits="StaticText"/>
                                      </XCUIElementTypeButton>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="698" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="698" width="362" height="54" traits=""/>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="20" y="750" width="362" height="2" traits=""/>
                                </XCUIElementTypeCell>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="751" width="402" height="19" traits=""/>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="769" width="402" height="18" traits=""/>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="786" width="402" height="19" traits=""/>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="804" width="402" height="18" traits=""/>
                                <XCUIElementTypeCell type="XCUIElementTypeCell" name="CERT_TRUST_SETTINGS" label="Certificate Trust Settings" enabled="true" visible="true" x="20" y="821" width="362" height="54" traits="Button">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="821" width="332" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="821" width="332" height="54" traits="">
                                      <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Certificate Trust Settings" name="Certificate Trust Settings" label="Certificate Trust Settings" enabled="true" visible="true" x="36" y="837" width="190" height="22" traits="StaticText"/>
                                    </XCUIElementTypeOther>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="821" width="362" height="54" traits="">
                                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="20" y="821" width="362" height="54" traits=""/>
                                  </XCUIElementTypeOther>
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="20" y="873" width="362" height="2" traits=""/>
                                  <XCUIElementTypeButton type="XCUIElementTypeButton" name="chevron" label="chevron" enabled="false" visible="false" x="351" y="840" width="11" height="15" traits="NotEnabled, Button"/>
                                </XCUIElementTypeCell>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="822" width="402" height="18" traits=""/>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" value="0%" name="Vertical scroll bar, 2 pages" label="Vertical scroll bar, 2 pages" enabled="true" visible="false" x="369" y="116" width="30" height="696" traits="Adjustable">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="396" y="119" width="3" height="638" traits=""/>
                                </XCUIElementTypeOther>
                                <XCUIElementTypeOther type="XCUIElementTypeOther" value="0%" name="Horizontal scroll bar, 1 page" label="Horizontal scroll bar, 1 page" enabled="true" visible="false" x="62" y="841" width="278" height="30" traits="Adjustable">
                                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="65" y="868" width="272" height="3" traits=""/>
                                </XCUIElementTypeOther>
                              </XCUIElementTypeTable>
                            </XCUIElementTypeOther>
                          </XCUIElementTypeOther>
                        </XCUIElementTypeOther>
                      </XCUIElementTypeOther>
                    </XCUIElementTypeOther>
                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="0" y="0" width="402" height="874" traits="">
                      <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="0" y="0" width="402" height="874" traits=""/>
                    </XCUIElementTypeOther>
                  </XCUIElementTypeOther>
                </XCUIElementTypeOther>
              </XCUIElementTypeOther>
              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits=""/>
              </XCUIElementTypeOther>
            </XCUIElementTypeOther>
          </XCUIElementTypeOther>
        </XCUIElementTypeOther>
      </XCUIElementTypeOther>
    </XCUIElementTypeOther>
  </XCUIElementTypeWindow>
</XCUIElementTypeApplication>
'''

# a SwiftUI app whose three pages share one frame in a ZStack; two are hidden with opacity(0), so WDA reports their content visible="false"
HIDDEN_PAGES = r'''<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="Tabs" label="Tabs" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="" processId="1000" bundleId="dev.mobster.tabsfixture">
  <XCUIElementTypeWindow type="XCUIElementTypeWindow" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
      <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
        <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
            <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Morning list" name="Morning list" label="Morning list" enabled="true" visible="false" x="130" y="386" width="142" height="34" traits="StaticText"/>
            <XCUIElementTypeButton type="XCUIElementTypeButton" name="add_habit" label="Add habit" enabled="true" visible="false" x="164" y="435" width="74" height="21" traits="Button"/>
            <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Weekly summary" name="Weekly summary" label="Weekly summary" enabled="true" visible="true" x="97" y="386" width="208" height="34" traits="StaticText"/>
            <XCUIElementTypeButton type="XCUIElementTypeButton" name="share_week" label="Share week" enabled="true" visible="true" x="157" y="435" width="89" height="21" traits="Button"/>
            <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Preferences" name="Preferences" label="Preferences" enabled="true" visible="false" x="129" y="386" width="144" height="34" traits="StaticText"/>
            <XCUIElementTypeButton type="XCUIElementTypeButton" name="reset_data" label="Reset data" enabled="true" visible="false" x="160" y="435" width="82" height="21" traits="Button"/>
            <XCUIElementTypeButton type="XCUIElementTypeButton" name="Today" label="Today" enabled="true" visible="true" x="41" y="800" width="47" height="21" traits="Button"/>
            <XCUIElementTypeButton type="XCUIElementTypeButton" name="Week" label="Week" enabled="true" visible="true" x="179" y="800" width="44" height="21" traits="Button"/>
            <XCUIElementTypeButton type="XCUIElementTypeButton" name="Settings" label="Settings" enabled="true" visible="true" x="306" y="800" width="64" height="21" traits="Button"/>
          </XCUIElementTypeOther>
        </XCUIElementTypeOther>
      </XCUIElementTypeOther>
    </XCUIElementTypeOther>
  </XCUIElementTypeWindow>
</XCUIElementTypeApplication>
'''

# a SwiftUI TabView on its second tab (iOS 26 keeps only the selected page)
TAB_VIEW = r'''<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="Tabs" label="Tabs" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="" processId="1000" bundleId="dev.mobster.tabsfixture">
  <XCUIElementTypeWindow type="XCUIElementTypeWindow" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
      <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
        <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                      <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="97" y="391" width="208" height="71" traits="">
                        <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Weekly summary" name="Weekly summary" label="Weekly summary" enabled="true" visible="true" x="97" y="391" width="208" height="35" traits="StaticText"/>
                        <XCUIElementTypeButton type="XCUIElementTypeButton" name="share_week" label="Share week" enabled="true" visible="true" x="157" y="441" width="89" height="21" traits="Button"/>
                      </XCUIElementTypeOther>
                    </XCUIElementTypeOther>
                  </XCUIElementTypeOther>
                </XCUIElementTypeOther>
              </XCUIElementTypeOther>
              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="791" width="402" height="83" traits="">
                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                  <XCUIElementTypeTabBar type="XCUIElementTypeTabBar" name="Tab Bar" label="Tab Bar" enabled="true" visible="true" x="0" y="791" width="402" height="83" traits="">
                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="64" y="791" width="274" height="62" traits="">
                      <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="64" y="791" width="94" height="54" traits=""/>
                      <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="64" y="791" width="274" height="62" traits="">
                        <XCUIElementTypeButton type="XCUIElementTypeButton" name="sun.max" label="Today" enabled="true" visible="true" x="68" y="795" width="94" height="54" traits="Button">
                          <XCUIElementTypeImage type="XCUIElementTypeImage" name="sun.max.fill" label="brightness higher" enabled="true" visible="true" x="101" y="800" width="29" height="30" traits="Image"/>
                        </XCUIElementTypeButton>
                        <XCUIElementTypeButton type="XCUIElementTypeButton" value="1" name="calendar" label="Week" enabled="true" visible="true" x="154" y="795" width="94" height="54" traits="Selected, Button">
                          <XCUIElementTypeImage type="XCUIElementTypeImage" name="calendar" label="calendar" enabled="true" visible="true" x="187" y="802" width="29" height="26" traits="Image"/>
                        </XCUIElementTypeButton>
                        <XCUIElementTypeButton type="XCUIElementTypeButton" name="gear" label="Settings" enabled="true" visible="true" x="240" y="795" width="94" height="54" traits="Button">
                          <XCUIElementTypeImage type="XCUIElementTypeImage" name="gear" label="settings" enabled="true" visible="true" x="273" y="800" width="29" height="30" traits="Image"/>
                        </XCUIElementTypeButton>
                      </XCUIElementTypeOther>
                      <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="154" y="795" width="94" height="54" traits="">
                        <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="154" y="795" width="94" height="54" traits="">
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="154" y="795" width="94" height="54" traits=""/>
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="154" y="795" width="94" height="54" traits="">
                            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="154" y="795" width="94" height="54" traits="">
                              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="154" y="795" width="94" height="54" traits="">
                                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="154" y="795" width="94" height="54" traits=""/>
                              </XCUIElementTypeOther>
                              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="154" y="795" width="94" height="54" traits=""/>
                            </XCUIElementTypeOther>
                            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="154" y="795" width="94" height="54" traits=""/>
                          </XCUIElementTypeOther>
                        </XCUIElementTypeOther>
                      </XCUIElementTypeOther>
                    </XCUIElementTypeOther>
                  </XCUIElementTypeTabBar>
                </XCUIElementTypeOther>
                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="791" width="402" height="83" traits="">
                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="791" width="402" height="83" traits="">
                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="791" width="402" height="83" traits="">
                      <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="791" width="402" height="83" traits="">
                        <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="791" width="402" height="83" traits="">
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="791" width="402" height="83" traits=""/>
                        </XCUIElementTypeOther>
                      </XCUIElementTypeOther>
                      <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="791" width="402" height="83" traits="">
                        <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="791" width="402" height="83" traits=""/>
                      </XCUIElementTypeOther>
                    </XCUIElementTypeOther>
                  </XCUIElementTypeOther>
                </XCUIElementTypeOther>
              </XCUIElementTypeOther>
            </XCUIElementTypeOther>
          </XCUIElementTypeOther>
        </XCUIElementTypeOther>
      </XCUIElementTypeOther>
    </XCUIElementTypeOther>
  </XCUIElementTypeWindow>
</XCUIElementTypeApplication>
'''


# Daybreak (examples/ios/Daybreak) > Settings: a plain SwiftUI Form. WDA reports its CollectionView visible="false"
# while every row inside is visible="true"
DAYBREAK_SETTINGS = r'''<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="Daybreak" label="Daybreak" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="" processId="1000" bundleId="dev.mobster.daybreak">
  <XCUIElementTypeWindow type="XCUIElementTypeWindow" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
      <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
        <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
              <XCUIElementTypeNavigationBar type="XCUIElementTypeNavigationBar" name="Settings" enabled="true" visible="true" x="0" y="62" width="402" height="106" traits="">
                <XCUIElementTypeButton type="XCUIElementTypeButton" name="BackButton" label="Back" enabled="true" visible="true" x="16" y="62" width="44" height="44" traits="Button"/>
                <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Settings" name="Settings" label="Settings" enabled="true" visible="true" x="16" y="119" width="133" height="42" traits="Header"/>
              </XCUIElementTypeNavigationBar>
              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                      <XCUIElementTypeCollectionView type="XCUIElementTypeCollectionView" enabled="true" visible="false" x="0" y="0" width="402" height="874" traits="">
                        <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="-16" y="-1580" width="434" height="1936" traits=""/>
                        <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits=""/>
                        <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits=""/>
                        <XCUIElementTypeCell type="XCUIElementTypeCell" enabled="true" visible="true" x="16" y="168" width="370" height="41" traits="Header">
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="168" width="370" height="41" traits=""/>
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="168" width="370" height="41" traits="">
                            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="168" width="370" height="41" traits="">
                              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="168" width="370" height="41" traits="">
                                <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Reminders" name="Reminders" label="Reminders" enabled="true" visible="true" x="16" y="168" width="370" height="41" traits="Header, StaticText"/>
                              </XCUIElementTypeOther>
                            </XCUIElementTypeOther>
                          </XCUIElementTypeOther>
                        </XCUIElementTypeCell>
                        <XCUIElementTypeCell type="XCUIElementTypeCell" enabled="true" visible="true" x="16" y="208" width="370" height="53" traits="">
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="208" width="370" height="53" traits="">
                            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="208" width="370" height="53" traits=""/>
                          </XCUIElementTypeOther>
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="208" width="370" height="53" traits="">
                            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="208" width="370" height="53" traits="">
                              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="208" width="370" height="53" traits="">
                                <XCUIElementTypeSwitch type="XCUIElementTypeSwitch" value="0" name="daily_reminder" label="Daily reminder" enabled="true" visible="true" x="16" y="208" width="370" height="53" traits="ToggleButton, Button">
                                  <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Daily reminder" name="Daily reminder" label="Daily reminder" enabled="true" visible="true" x="32" y="224" width="110" height="21" traits="StaticText"/>
                                  <XCUIElementTypeSwitch type="XCUIElementTypeSwitch" value="0" enabled="true" visible="true" x="309" y="220" width="63" height="29" traits="ToggleButton, Button"/>
                                </XCUIElementTypeSwitch>
                              </XCUIElementTypeOther>
                            </XCUIElementTypeOther>
                          </XCUIElementTypeOther>
                        </XCUIElementTypeCell>
                        <XCUIElementTypeCell type="XCUIElementTypeCell" enabled="true" visible="true" x="16" y="260" width="370" height="67" traits="">
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="260" width="370" height="67" traits="">
                            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="260" width="370" height="67" traits=""/>
                          </XCUIElementTypeOther>
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="260" width="370" height="67" traits="">
                            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="260" width="370" height="67" traits="">
                              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="260" width="370" height="67" traits="">
                                <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Reminder time" name="Reminder time" label="Reminder time" enabled="false" visible="true" x="32" y="283" width="111" height="21" traits="NotEnabled, StaticText"/>
                                <XCUIElementTypeDatePicker type="XCUIElementTypeDatePicker" enabled="false" visible="true" x="283" y="275" width="87" height="37" traits="NotEnabled">
                                  <XCUIElementTypeButton type="XCUIElementTypeButton" value="7:30 AM" name="Time Picker" label="Time Picker" enabled="false" visible="true" x="283" y="275" width="87" height="37" traits="NotEnabled, Button"/>
                                </XCUIElementTypeDatePicker>
                              </XCUIElementTypeOther>
                            </XCUIElementTypeOther>
                          </XCUIElementTypeOther>
                        </XCUIElementTypeCell>
                        <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="32" y="259" width="338" height="2" traits=""/>
                        <XCUIElementTypeCell type="XCUIElementTypeCell" enabled="true" visible="true" x="16" y="326" width="370" height="31" traits="">
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="326" width="370" height="31" traits=""/>
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="326" width="370" height="31" traits="">
                            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="326" width="370" height="31" traits="">
                              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="326" width="370" height="31" traits="">
                                <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Daybreak sends one notification a day at this time." name="Daybreak sends one notification a day at this time." label="Daybreak sends one notification a day at this time." enabled="true" visible="true" x="16" y="326" width="370" height="31" traits="StaticText"/>
                              </XCUIElementTypeOther>
                            </XCUIElementTypeOther>
                          </XCUIElementTypeOther>
                        </XCUIElementTypeCell>
                        <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="-16" y="356" width="434" height="110" traits=""/>
                        <XCUIElementTypeCell type="XCUIElementTypeCell" enabled="true" visible="true" x="16" y="356" width="370" height="41" traits="Header">
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="356" width="370" height="41" traits=""/>
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="356" width="370" height="41" traits="">
                            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="356" width="370" height="41" traits="">
                              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="356" width="370" height="41" traits="">
                                <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Daybreak Plus" name="Daybreak Plus" label="Daybreak Plus" enabled="true" visible="true" x="16" y="356" width="370" height="41" traits="Header, StaticText"/>
                              </XCUIElementTypeOther>
                            </XCUIElementTypeOther>
                          </XCUIElementTypeOther>
                        </XCUIElementTypeCell>
                        <XCUIElementTypeCell type="XCUIElementTypeCell" enabled="true" visible="true" x="16" y="396" width="370" height="53" traits="">
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="396" width="370" height="53" traits="">
                            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="396" width="370" height="53" traits=""/>
                          </XCUIElementTypeOther>
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="396" width="370" height="53" traits="">
                            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="396" width="370" height="53" traits="">
                              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="396" width="370" height="53" traits="">
                                <XCUIElementTypeButton type="XCUIElementTypeButton" name="settings_paywall" label="Show paywall" enabled="true" visible="true" x="16" y="396" width="370" height="53" traits="Button">
                                  <XCUIElementTypeImage type="XCUIElementTypeImage" name="sparkles" label="Sparkle" enabled="true" visible="true" x="34" y="410" width="20" height="25" traits="Image"/>
                                  <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Show paywall" name="Show paywall" label="Show paywall" enabled="true" visible="true" x="34" y="410" width="141" height="25" traits="StaticText"/>
                                  <XCUIElementTypeImage type="XCUIElementTypeImage" name="chevron.right" label="Forward" enabled="true" visible="true" x="362" y="416" width="8" height="13" traits="Image"/>
                                </XCUIElementTypeButton>
                              </XCUIElementTypeOther>
                            </XCUIElementTypeOther>
                          </XCUIElementTypeOther>
                        </XCUIElementTypeCell>
                        <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="-16" y="466" width="434" height="1913" traits=""/>
                        <XCUIElementTypeCell type="XCUIElementTypeCell" enabled="true" visible="true" x="16" y="466" width="370" height="41" traits="Header">
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="466" width="370" height="41" traits=""/>
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="466" width="370" height="41" traits="">
                            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="466" width="370" height="41" traits="">
                              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="466" width="370" height="41" traits="">
                                <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="About" name="About" label="About" enabled="true" visible="true" x="16" y="466" width="370" height="41" traits="Header, StaticText"/>
                              </XCUIElementTypeOther>
                            </XCUIElementTypeOther>
                          </XCUIElementTypeOther>
                        </XCUIElementTypeCell>
                        <XCUIElementTypeCell type="XCUIElementTypeCell" enabled="true" visible="true" x="16" y="506" width="370" height="53" traits="">
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="506" width="370" height="53" traits="">
                            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="506" width="370" height="53" traits=""/>
                          </XCUIElementTypeOther>
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="506" width="370" height="53" traits="">
                            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="506" width="370" height="53" traits="">
                              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="506" width="370" height="53" traits="">
                                <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Version" name="Version" label="Version" enabled="true" visible="false" x="32" y="522" width="57" height="21" traits="StaticText"/>
                                <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Version, 1.0 (1)" name="Version, 1.0 (1)" label="Version, 1.0 (1)" enabled="true" visible="true" x="16" y="506" width="370" height="53" traits="StaticText"/>
                              </XCUIElementTypeOther>
                            </XCUIElementTypeOther>
                          </XCUIElementTypeOther>
                        </XCUIElementTypeCell>
                        <XCUIElementTypeCell type="XCUIElementTypeCell" enabled="true" visible="true" x="16" y="558" width="370" height="53" traits="">
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="558" width="370" height="53" traits="">
                            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="16" y="558" width="370" height="53" traits=""/>
                          </XCUIElementTypeOther>
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="558" width="370" height="53" traits="">
                            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="558" width="370" height="53" traits="">
                              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="16" y="558" width="370" height="53" traits="">
                                <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Data" name="Data" label="Data" enabled="true" visible="false" x="32" y="574" width="36" height="21" traits="StaticText"/>
                                <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Data, On this iPhone only" name="Data, On this iPhone only" label="Data, On this iPhone only" enabled="true" visible="true" x="16" y="558" width="370" height="53" traits="StaticText"/>
                              </XCUIElementTypeOther>
                            </XCUIElementTypeOther>
                          </XCUIElementTypeOther>
                        </XCUIElementTypeCell>
                        <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="32" y="557" width="338" height="2" traits=""/>
                        <XCUIElementTypeOther type="XCUIElementTypeOther" value="0%" name="Vertical scroll bar, 1 page" label="Vertical scroll bar, 1 page" enabled="true" visible="true" x="369" y="116" width="30" height="696" traits="Adjustable">
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="396" y="328" width="3" height="481" traits=""/>
                        </XCUIElementTypeOther>
                        <XCUIElementTypeOther type="XCUIElementTypeOther" value="0%" name="Vertical scroll bar, 1 page" label="Vertical scroll bar, 1 page" enabled="true" visible="true" x="369" y="116" width="30" height="696" traits="Adjustable">
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="396" y="328" width="3" height="481" traits=""/>
                        </XCUIElementTypeOther>
                      </XCUIElementTypeCollectionView>
                    </XCUIElementTypeOther>
                  </XCUIElementTypeOther>
                </XCUIElementTypeOther>
              </XCUIElementTypeOther>
              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="0" y="0" width="402" height="874" traits="">
                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="0" y="0" width="402" height="874" traits=""/>
              </XCUIElementTypeOther>
            </XCUIElementTypeOther>
          </XCUIElementTypeOther>
        </XCUIElementTypeOther>
      </XCUIElementTypeOther>
    </XCUIElementTypeOther>
  </XCUIElementTypeWindow>
</XCUIElementTypeApplication>

'''

# Daybreak's paywall, a fullScreenCover over onboarding: every covered onboarding node is itself visible="false"
DAYBREAK_PAYWALL = r'''<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="Daybreak" label="Daybreak" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="" processId="1000" bundleId="dev.mobster.daybreak">
  <XCUIElementTypeWindow type="XCUIElementTypeWindow" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="0" y="0" width="402" height="874" traits="">
      <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="0" y="0" width="402" height="874" traits="">
        <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="0" y="0" width="402" height="874" traits="">
          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="0" y="0" width="402" height="874" traits="">
            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="0" y="0" width="402" height="874" traits=""/>
            <XCUIElementTypeImage type="XCUIElementTypeImage" name="drop.fill" label="Water Drop" enabled="true" visible="false" x="66" y="218" width="12" height="18" traits="Image"/>
            <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Drink water" name="Drink water" label="Drink water" enabled="true" visible="false" x="106" y="216" width="90" height="21" traits="StaticText"/>
            <XCUIElementTypeImage type="XCUIElementTypeImage" name="checkmark.circle.fill" label="Selected" enabled="true" visible="false" x="324" y="214" width="24" height="25" traits="Selected, Image"/>
            <XCUIElementTypeImage type="XCUIElementTypeImage" name="book.fill" label="Bookmark" enabled="true" visible="false" x="67" y="299" width="19" height="15" traits="Image"/>
            <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Read 10 pages" name="Read 10 pages" label="Read 10 pages" enabled="true" visible="false" x="109" y="296" width="112" height="21" traits="StaticText"/>
            <XCUIElementTypeImage type="XCUIElementTypeImage" name="checkmark.circle.fill" label="Selected" enabled="true" visible="false" x="319" y="295" width="24" height="24" traits="Selected, Image"/>
            <XCUIElementTypeImage type="XCUIElementTypeImage" name="figure.walk" label="Walk" enabled="true" visible="false" x="75" y="377" width="12" height="19" traits="Image"/>
            <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Walk 5,000 steps" name="Walk 5,000 steps" label="Walk 5,000 steps" enabled="true" visible="false" x="112" y="377" width="130" height="20" traits="StaticText"/>
            <XCUIElementTypeImage type="XCUIElementTypeImage" name="circle" label="circle" enabled="true" visible="false" x="315" y="375" width="23" height="23" traits="Image"/>
            <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Build habits that stick" name="Build habits that stick" label="Build habits that stick" enabled="true" visible="false" x="40" y="551" width="322" height="39" traits="Header, StaticText"/>
            <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Small daily wins add up. Daybreak keeps your routine short and your progress in view." name="Small daily wins add up. Daybreak keeps your routine short and your progress in view." label="Small daily wins add up. Daybreak keeps your routine short and your progress in view." enabled="true" visible="false" x="32" y="603" width="338" height="44" traits="StaticText"/>
            <XCUIElementTypeOther type="XCUIElementTypeOther" name="Page 1 of 3" label="Page 1 of 3" enabled="true" visible="false" x="177" y="741" width="48" height="7" traits=""/>
            <XCUIElementTypeButton type="XCUIElementTypeButton" name="onboarding_continue" label="Continue" enabled="true" visible="false" x="24" y="772" width="354" height="56" traits="Button"/>
          </XCUIElementTypeOther>
        </XCUIElementTypeOther>
      </XCUIElementTypeOther>
    </XCUIElementTypeOther>
    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
      <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
        <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="0" y="0" width="402" height="874" traits=""/>
          <XCUIElementTypeScrollView type="XCUIElementTypeScrollView" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="96" width="402" height="593" traits="">
              <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="DAYBREAK PLUS" name="DAYBREAK PLUS" label="DAYBREAK PLUS" enabled="true" visible="true" x="140" y="188" width="122" height="15" traits="StaticText"/>
              <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Choose your plan" name="paywall_title" label="Choose your plan" enabled="true" visible="true" x="79" y="210" width="244" height="37" traits="Header, StaticText"/>
              <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Unlimited habits, streak insights and reminders that fit your day." name="Unlimited habits, streak insights and reminders that fit your day." label="Unlimited habits, streak insights and reminders that fit your day." enabled="true" visible="true" x="67" y="254" width="268" height="41" traits="StaticText"/>
              <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Unlimited habits" name="Unlimited habits" label="Unlimited habits" enabled="true" visible="true" x="74" y="330" width="115" height="19" traits="StaticText"/>
              <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Streak insights and history" name="Streak insights and history" label="Streak insights and history" enabled="true" visible="true" x="74" y="366" width="188" height="19" traits="StaticText"/>
              <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Reminders at the time you choose" name="Reminders at the time you choose" label="Reminders at the time you choose" enabled="true" visible="true" x="74" y="402" width="238" height="19" traits="StaticText"/>
              <XCUIElementTypeButton type="XCUIElementTypeButton" value="$2.99 / week" name="plan_weekly" label="Weekly" enabled="true" visible="true" x="20" y="464" width="362" height="65" traits="Button">
                <XCUIElementTypeImage type="XCUIElementTypeImage" name="circle" label="circle" enabled="true" visible="true" x="38" y="485" width="23" height="24" traits="Image"/>
                <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Weekly" name="Weekly" label="Weekly" enabled="true" visible="true" x="76" y="477" width="58" height="21" traits="StaticText"/>
                <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Billed every week" name="Billed every week" label="Billed every week" enabled="true" visible="true" x="76" y="500" width="106" height="17" traits="StaticText"/>
                <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="$2.99" name="$2.99" label="$2.99" enabled="true" visible="true" x="308" y="476" width="58" height="25" traits="StaticText"/>
                <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="/ week" name="/ week" label="/ week" enabled="true" visible="true" x="329" y="501" width="37" height="16" traits="StaticText"/>
              </XCUIElementTypeButton>
              <XCUIElementTypeButton type="XCUIElementTypeButton" value="$7.99 / month" name="plan_monthly" label="Monthly" enabled="true" visible="true" x="20" y="538" width="362" height="65" traits="Button">
                <XCUIElementTypeImage type="XCUIElementTypeImage" name="circle" label="circle" enabled="true" visible="true" x="38" y="559" width="23" height="24" traits="Image"/>
                <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Monthly" name="Monthly" label="Monthly" enabled="true" visible="true" x="76" y="551" width="65" height="21" traits="StaticText"/>
                <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Billed every month" name="Billed every month" label="Billed every month" enabled="true" visible="true" x="76" y="574" width="113" height="17" traits="StaticText"/>
                <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="$7.99" name="$7.99" label="$7.99" enabled="true" visible="true" x="308" y="551" width="58" height="24" traits="StaticText"/>
                <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="/ month" name="/ month" label="/ month" enabled="true" visible="true" x="323" y="576" width="43" height="15" traits="StaticText"/>
              </XCUIElementTypeButton>
              <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Save 58%" name="Save 58%" label="Save 58%" enabled="true" visible="true" x="303" y="606" width="57" height="14" traits="StaticText"/>
              <XCUIElementTypeButton type="XCUIElementTypeButton" value="$39.99 / year" name="plan_annual" label="Annual" enabled="true" visible="true" x="20" y="612" width="362" height="65" traits="Selected, Button">
                <XCUIElementTypeImage type="XCUIElementTypeImage" name="checkmark.circle.fill" label="Selected" enabled="true" visible="true" x="38" y="633" width="23" height="24" traits="Selected, Image"/>
                <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Annual" name="Annual" label="Annual" enabled="true" visible="true" x="76" y="625" width="56" height="21" traits="StaticText"/>
                <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="$3.33 a month, billed yearly" name="$3.33 a month, billed yearly" label="$3.33 a month, billed yearly" enabled="true" visible="true" x="76" y="648" width="169" height="17" traits="StaticText"/>
                <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="$39.99" name="$39.99" label="$39.99" enabled="true" visible="true" x="295" y="625" width="71" height="24" traits="StaticText"/>
                <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="/ year" name="/ year" label="/ year" enabled="true" visible="true" x="334" y="650" width="32" height="15" traits="StaticText"/>
              </XCUIElementTypeButton>
            </XCUIElementTypeOther>
            <XCUIElementTypeOther type="XCUIElementTypeOther" value="0%" name="Vertical scroll bar, 1 page" label="Vertical scroll bar, 1 page" enabled="true" visible="true" x="369" y="96" width="30" height="615" traits="Adjustable">
              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="396" y="121" width="3" height="587" traits=""/>
            </XCUIElementTypeOther>
            <XCUIElementTypeOther type="XCUIElementTypeOther" value="0%" name="Vertical scroll bar, 1 page" label="Vertical scroll bar, 1 page" enabled="true" visible="true" x="369" y="96" width="30" height="615" traits="Adjustable">
              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="396" y="121" width="3" height="587" traits=""/>
            </XCUIElementTypeOther>
          </XCUIElementTypeScrollView>
          <XCUIElementTypeButton type="XCUIElementTypeButton" name="paywall_close" label="Not now" enabled="true" visible="true" x="321" y="70" width="59" height="18" traits="Button"/>
          <XCUIElementTypeButton type="XCUIElementTypeButton" name="paywall_cta" label="Start free trial" enabled="true" visible="true" x="20" y="724" width="362" height="57" traits="Button"/>
          <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="7 days free, then $39.99 / year. Cancel anytime." name="7 days free, then $39.99 / year. Cancel anytime." label="7 days free, then $39.99 / year. Cancel anytime." enabled="true" visible="true" x="56" y="790" width="290" height="17" traits="StaticText"/>
          <XCUIElementTypeButton type="XCUIElementTypeButton" name="paywall_restore" label="Restore Purchases" enabled="true" visible="true" x="77" y="818" width="117" height="16" traits="Button"/>
          <XCUIElementTypeLink type="XCUIElementTypeLink" name="Terms" label="Terms" enabled="true" visible="true" x="217" y="818" width="39" height="16" traits="Link"/>
          <XCUIElementTypeLink type="XCUIElementTypeLink" name="Privacy" label="Privacy" enabled="true" visible="true" x="279" y="818" width="47" height="16" traits="Link"/>
        </XCUIElementTypeOther>
      </XCUIElementTypeOther>
    </XCUIElementTypeOther>
  </XCUIElementTypeWindow>
</XCUIElementTypeApplication>

'''

# Daybreak's Today: each SwiftUI Toggle reads as a labelled Switch (the row) around an unlabelled Switch (the knob)
DAYBREAK_TODAY = r'''<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="Daybreak" label="Daybreak" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="" processId="1000" bundleId="dev.mobster.daybreak">
  <XCUIElementTypeWindow type="XCUIElementTypeWindow" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
      <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
        <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
            <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                  <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                    <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                      <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="0" y="0" width="402" height="874" traits=""/>
                      <XCUIElementTypeScrollView type="XCUIElementTypeScrollView" enabled="true" visible="true" x="0" y="0" width="402" height="874" traits="">
                        <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="0" y="62" width="402" height="598" traits="">
                          <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="MONDAY, SEPTEMBER 28" name="MONDAY, SEPTEMBER 28" label="MONDAY, SEPTEMBER 28" enabled="true" visible="true" x="20" y="70" width="175" height="15" traits="StaticText"/>
                          <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Today" name="Today" label="Today" enabled="true" visible="true" x="20" y="86" width="96" height="41" traits="Header, StaticText"/>
                          <XCUIElementTypeButton type="XCUIElementTypeButton" name="open_settings" label="Settings" enabled="true" visible="true" x="342" y="70" width="40" height="40" traits="Button"/>
                          <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="12-day streak" name="streak_count" label="12-day streak" enabled="true" visible="true" x="104" y="166" width="130" height="25" traits="StaticText"/>
                          <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="3 left to keep it going." name="3 left to keep it going." label="3 left to keep it going." enabled="true" visible="true" x="104" y="193" width="150" height="19" traits="StaticText"/>
                          <XCUIElementTypeOther type="XCUIElementTypeOther" name="0 of 3 done" label="0 of 3 done" enabled="true" visible="true" x="315" y="162" width="54" height="54" traits="">
                            <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="0/3" name="0/3" label="0/3" enabled="true" visible="true" x="330" y="181" width="24" height="16" traits="StaticText"/>
                          </XCUIElementTypeOther>
                          <XCUIElementTypeOther type="XCUIElementTypeOther" value="6 of 7 done" name="Last 7 days" label="Last 7 days" enabled="true" visible="true" x="20" y="251" width="362" height="83" traits="">
                            <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="T" name="T" label="T" enabled="true" visible="true" x="41" y="265" width="9" height="15" traits="StaticText"/>
                            <XCUIElementTypeImage type="XCUIElementTypeImage" name="checkmark" label="Selected" enabled="true" visible="true" x="41" y="298" width="10" height="11" traits="Selected, Image"/>
                            <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="W" name="W" label="W" enabled="true" visible="true" x="91" y="265" width="13" height="15" traits="StaticText"/>
                            <XCUIElementTypeImage type="XCUIElementTypeImage" name="checkmark" label="Selected" enabled="true" visible="true" x="92" y="298" width="11" height="11" traits="Selected, Image"/>
                            <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="T" name="T" label="T" enabled="true" visible="true" x="145" y="265" width="9" height="15" traits="StaticText"/>
                            <XCUIElementTypeImage type="XCUIElementTypeImage" name="checkmark" label="Selected" enabled="true" visible="true" x="144" y="298" width="11" height="11" traits="Selected, Image"/>
                            <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="F" name="F" label="F" enabled="true" visible="true" x="197" y="265" width="8" height="15" traits="StaticText"/>
                            <XCUIElementTypeImage type="XCUIElementTypeImage" name="checkmark" label="Selected" enabled="true" visible="true" x="196" y="298" width="10" height="11" traits="Selected, Image"/>
                            <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="S" name="S" label="S" enabled="true" visible="true" x="248" y="265" width="9" height="15" traits="StaticText"/>
                            <XCUIElementTypeImage type="XCUIElementTypeImage" name="checkmark" label="Selected" enabled="true" visible="true" x="247" y="298" width="11" height="11" traits="Selected, Image"/>
                            <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="S" name="S" label="S" enabled="true" visible="true" x="300" y="265" width="9" height="15" traits="StaticText"/>
                            <XCUIElementTypeImage type="XCUIElementTypeImage" name="checkmark" label="Selected" enabled="true" visible="true" x="299" y="298" width="11" height="11" traits="Selected, Image"/>
                            <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="M" name="M" label="M" enabled="true" visible="true" x="350" y="265" width="12" height="15" traits="StaticText"/>
                            <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="28" name="28" label="28" enabled="true" visible="true" x="348" y="296" width="16" height="14" traits="StaticText"/>
                          </XCUIElementTypeOther>
                          <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Habits" name="Habits" label="Habits" enabled="true" visible="true" x="20" y="357" width="60" height="25" traits="Header, StaticText"/>
                          <XCUIElementTypeSwitch type="XCUIElementTypeSwitch" value="0" name="habit_water" label="Drink water" enabled="true" visible="true" x="92" y="408" width="276" height="39" traits="ToggleButton, Button">
                            <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Drink water" name="Drink water" label="Drink water" enabled="true" visible="true" x="92" y="408" width="90" height="21" traits="StaticText"/>
                            <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="8 glasses" name="8 glasses" label="8 glasses" enabled="true" visible="true" x="92" y="430" width="58" height="17" traits="StaticText"/>
                            <XCUIElementTypeSwitch type="XCUIElementTypeSwitch" value="0" enabled="true" visible="true" x="307" y="413" width="63" height="29" traits="ToggleButton, Button"/>
                          </XCUIElementTypeSwitch>
                          <XCUIElementTypeSwitch type="XCUIElementTypeSwitch" value="0" name="habit_read" label="Read 10 pages" enabled="true" visible="true" x="92" y="490" width="276" height="39" traits="ToggleButton, Button">
                            <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Read 10 pages" name="Read 10 pages" label="Read 10 pages" enabled="true" visible="true" x="92" y="490" width="116" height="21" traits="StaticText"/>
                            <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Before bed" name="Before bed" label="Before bed" enabled="true" visible="true" x="92" y="512" width="67" height="17" traits="StaticText"/>
                            <XCUIElementTypeSwitch type="XCUIElementTypeSwitch" value="0" enabled="true" visible="true" x="307" y="495" width="63" height="29" traits="ToggleButton, Button"/>
                          </XCUIElementTypeSwitch>
                          <XCUIElementTypeSwitch type="XCUIElementTypeSwitch" value="0" name="habit_walk" label="Walk 5,000 steps" enabled="true" visible="true" x="92" y="572" width="276" height="39" traits="ToggleButton, Button">
                            <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Walk 5,000 steps" name="Walk 5,000 steps" label="Walk 5,000 steps" enabled="true" visible="true" x="92" y="572" width="139" height="21" traits="StaticText"/>
                            <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Anytime today" name="Anytime today" label="Anytime today" enabled="true" visible="true" x="92" y="594" width="87" height="17" traits="StaticText"/>
                            <XCUIElementTypeSwitch type="XCUIElementTypeSwitch" value="0" enabled="true" visible="true" x="307" y="577" width="63" height="29" traits="ToggleButton, Button"/>
                          </XCUIElementTypeSwitch>
                        </XCUIElementTypeOther>
                        <XCUIElementTypeOther type="XCUIElementTypeOther" value="0%" name="Vertical scroll bar, 1 page" label="Vertical scroll bar, 1 page" enabled="true" visible="true" x="369" y="62" width="30" height="750" traits="Adjustable">
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="396" y="245" width="3" height="564" traits=""/>
                        </XCUIElementTypeOther>
                        <XCUIElementTypeOther type="XCUIElementTypeOther" value="0%" name="Vertical scroll bar, 1 page" label="Vertical scroll bar, 1 page" enabled="true" visible="true" x="369" y="62" width="30" height="750" traits="Adjustable">
                          <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="true" x="396" y="245" width="3" height="564" traits=""/>
                        </XCUIElementTypeOther>
                      </XCUIElementTypeScrollView>
                    </XCUIElementTypeOther>
                  </XCUIElementTypeOther>
                </XCUIElementTypeOther>
              </XCUIElementTypeOther>
              <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="0" y="0" width="402" height="874" traits="">
                <XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" visible="false" x="0" y="0" width="402" height="874" traits=""/>
              </XCUIElementTypeOther>
            </XCUIElementTypeOther>
          </XCUIElementTypeOther>
        </XCUIElementTypeOther>
      </XCUIElementTypeOther>
    </XCUIElementTypeOther>
  </XCUIElementTypeWindow>
</XCUIElementTypeApplication>

'''

ALL = {"settings-root": SETTINGS_ROOT, "settings-general": SETTINGS_GENERAL, "settings-about": SETTINGS_ABOUT,
       "hidden-pages": HIDDEN_PAGES, "tab-view": TAB_VIEW, "daybreak-settings": DAYBREAK_SETTINGS,
       "daybreak-paywall": DAYBREAK_PAYWALL, "daybreak-today": DAYBREAK_TODAY}


# -- synthetic trees ----------------------------------------------------------------------------------------------

def _escape(text):
    return (str(text).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;"))


def node(role, label=None, *, id=None, value=None, rect=(16, 100, 200, 44), visible=True, enabled=True, traits="",
         placeholder=None, children=()):
    """One WDA source element as the evidence read writes it: ``name`` is the identifier, else the label."""
    x, y, w, h = rect
    attrs = [f'type="XCUIElementType{role}"']
    if value is not None:
        attrs.append(f'value="{_escape(value)}"')
    name = id if id is not None else label
    if name is not None:
        attrs.append(f'name="{_escape(name)}"')
    if label is not None:
        attrs.append(f'label="{_escape(label)}"')
    if placeholder is not None:
        attrs.append(f'placeholderValue="{_escape(placeholder)}"')
    attrs.append(f'enabled="{"true" if enabled else "false"}"')
    if visible is not None:
        attrs.append(f'visible="{"true" if visible else "false"}"')
    attrs.append(f'x="{x}" y="{y}" width="{w}" height="{h}" traits="{_escape(traits)}"')
    head = f"<XCUIElementType{role} " + " ".join(attrs)
    if not children:
        return head + "/>"
    return head + ">" + "".join(children) + f"</XCUIElementType{role}>"


def app(*children, bundle="dev.mobster.fixture", width=402, height=874):
    """An Application and its Window around ``children``."""
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="Fixture" label="Fixture" '
            f'enabled="true" visible="true" x="0" y="0" width="{width}" height="{height}" traits="" '
            f'bundleId="{bundle}"><XCUIElementTypeWindow type="XCUIElementTypeWindow" enabled="true" visible="true" '
            f'x="0" y="0" width="{width}" height="{height}" traits="">' + "".join(children)
            + "</XCUIElementTypeWindow></XCUIElementTypeApplication>")


def paywall(plans=("Weekly", "Monthly", "Annual"), annual="$39.99 / year", loading=False):
    """Daybreak's paywall as a fictional tree: the sample app's identifiers, labels and values (SPEC §11.1)."""
    values = {"Weekly": "$2.99 / week", "Monthly": "$7.99 / month", "Annual": annual}
    rows = [node("StaticText", "Choose your plan", id="paywall_title", value="Choose your plan",
                 rect=(24, 118, 354, 34))]
    for index, plan in enumerate(plans):
        rows.append(node("Button", plan, id=f"plan_{plan.lower()}", value=values[plan],
                         rect=(24, 180 + index * 90, 354, 80),
                         traits="Button, Selected" if plan == "Annual" else "Button"))
    if "Annual" in plans:
        rows.append(node("StaticText", "Save 58%", rect=(280, 460, 90, 20)))
    if loading:
        rows.append(node("StaticText", "Loading plans…", rect=(24, 560, 200, 20)))
    rows += [node("Button", "Start free trial", id="paywall_cta", rect=(24, 700, 354, 50)),
             node("Button", "Restore Purchases", id="paywall_restore", rect=(24, 760, 170, 30)),
             node("Link", "Terms", rect=(220, 760, 60, 30)), node("Link", "Privacy", rect=(290, 760, 70, 30)),
             node("Button", "Not now", id="paywall_close", rect=(330, 60, 60, 30))]
    return app(node("Other", children=rows, rect=(0, 0, 402, 874)), bundle="dev.mobster.daybreak")


class FixturePrivacyTests(unittest.TestCase):
    def test_no_identifying_values(self):
        for name, xml in ALL.items():
            with self.subTest(fixture=name):
                self.assertNotRegex(xml, r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}")  # UDIDs
                self.assertNotRegex(xml, r"([0-9a-f]{2}:){5}[0-9a-f]{2}")  # hardware addresses
                self.assertNotRegex(xml, r"/Users/")
                self.assertIn(xml.count('processId="'), (0, 1))
                if 'processId="' in xml:
                    self.assertIn('processId="1000"', xml)

    def test_the_serial_number_is_a_placeholder(self):
        serial = re.search(r'label="Serial Number, ([^"]+)"', SETTINGS_ABOUT).group(1)
        self.assertEqual(serial, "SIM0SERIAL0")


if __name__ == "__main__":
    unittest.main()

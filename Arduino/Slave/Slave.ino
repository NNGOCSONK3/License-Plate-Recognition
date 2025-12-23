/*
  SLAVE RFID OUT
  - Reads RC522 and prints: RFID_OUT:<UID>
  - Serial TX(D1) -> MASTER A2
  - Disconnect TX while uploading if needed
*/

#include <SPI.h>
#include <MFRC522.h>

#define RFID_SS   10
#define RFID_RST  9
MFRC522 rfid(RFID_SS, RFID_RST);

static String uidToString(const MFRC522::Uid &uid) {
  String s;
  for (byte i = 0; i < uid.size; i++) {
    if (uid.uidByte[i] < 0x10) s += "0";
    s += String(uid.uidByte[i], HEX);
  }
  s.toUpperCase();
  return s;
}

void setup() {
  Serial.begin(9600);
  SPI.begin();
  rfid.PCD_Init();
  Serial.println("SLAVE_RFID_READY");
}

void loop() {
  if (rfid.PICC_IsNewCardPresent() && rfid.PICC_ReadCardSerial()) {
    String uid = uidToString(rfid.uid);
    Serial.print("RFID_OUT:");
    Serial.println(uid);
    rfid.PICC_HaltA();
    rfid.PCD_StopCrypto1();
  }
}

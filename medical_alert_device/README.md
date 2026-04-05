# Personal Monitoring System

## Overview
A modular embedded monitoring system designed to capture and transmit real-time motion and positional data across multiple body segments. The system consists of distributed sensor nodes (wrist, leg, and display units) that communicate wirelessly to provide a unified view of user movement.

## System Architecture
This project is composed of three independent embedded nodes:

- **Wrist Unit** – Captures motion and orientation data
- **Leg Unit** – Provides additional movement tracking
- **Display Unit** – Aggregates and presents system data

Each unit operates independently and communicates via RF.

## Features
- Multi-node distributed architecture  
- Real-time sensor data acquisition  
- RF-based wireless communication  
- Modular and extensible design  
- Low-power embedded implementation  

## Technologies
- Arduino (C/C++)  
- Motion sensors (IMU-based)  
- RF communication modules  
- Serial communication for debugging  

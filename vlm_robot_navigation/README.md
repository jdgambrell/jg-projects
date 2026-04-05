# VLM-Guided Robot Navigation (Embodied AI)

## Overview
Development of an embodied AI system enabling robots to interpret and interact with real-world environments using vision-language models (VLMs). This project integrates perception, reasoning, and navigation to enable interaction-aware behavior in complex, real-world scenarios.

The system combines VLM/LLM reasoning, CLIP-based object localization, and ROS2/Nav2 navigation to handle environments where traditional planners struggle—particularly in cases requiring semantic understanding and physical interaction (e.g., doors, buttons).

## Key Features
- Vision-language reasoning for environment understanding and decision-making  
- CLIP-based object localization for interaction-aware perception  
- ROS2 + Nav2 autonomous navigation with goal sequencing  
- Real-time blockage detection and recovery handling  
- Interactive behaviors (e.g., button detection and retry logic)  
- Integrated text-to-speech feedback for system transparency  

## System Architecture
The system operates as a closed-loop embodied agent:

1. **Perception Layer**
   - Camera-based image capture
   - LaserScan-based obstacle detection
   - Snapshot generation for VLM processing

2. **Reasoning Layer**
   - VLM/LLM-based scene interpretation
   - Object identification (e.g., doors, buttons)
   - Action recommendation (e.g., press, wait, reroute)

3. **Navigation Layer**
   - ROS2 Nav2 stack for path planning and execution
   - Goal sequencing with recovery behaviors
   - Dynamic re-tasking based on semantic feedback

4. **Interaction Layer**
   - Button-press protocol with retry logic
   - Event-driven responses to environmental conditions
   - TTS feedback for human-robot interaction


## How It Works
1. Robot navigates through predefined goal locations using Nav2  
2. Sensor data (camera + LiDAR) is continuously monitored  
3. When navigation fails or obstacles are detected:
   - A snapshot is captured and passed to a VLM-based assessor  
   - The model identifies obstacles and suggests actions  
4. If interaction is required (e.g., button press):
   - Robot executes a specialized interaction routine  
   - Retries are handled with bounded logic and safety constraints  
5. Navigation resumes toward the original goal


## Technologies
- ROS2 (rclpy)  
- Nav2 navigation stack  
- OpenCV (image capture and processing)  
- NumPy  
- Vision-Language Models (VLM/LLM integration)  
- CLIP (for object localization)  

## Simulation and Evaluation
- Tested in simulation environments (e.g., Isaac Sim)  
- Validated on real-world robotic platforms (Clearpath Jackal)  
- Designed for transfer between simulation and physical deployment  

## Research Context
This work contributes to ongoing research in embodied AI and interaction-aware navigation.

- Paper accepted to **CVPR 2026**  
- Extended work submitted to **IROS**  

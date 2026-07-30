# Next steps for the AI curriculum tutor

## What the app already does well
- Reads course materials and prepares lesson packs from textbook content.
- Presents a lesson experience with navigation, practice, assessment, and progress tracking.
- Supports a web UI and a terminal CLI.
- Uses AI to explain concepts and grade mastery.

## What should be added next
### 1. Make the AI act as a curriculum decomposer
The app should not only teach a unit; it should break each textbook chapter into simpler learning modules.

Each unit should generate:
- a short beginner-friendly summary
- prerequisite concepts
- core ideas to learn first
- common misconceptions
- worked examples
- a quick quiz
- a one-page study guide

### 2. Add a module map before lesson playback
Before the student starts a unit, show:
- why the topic matters
- what is required to understand it
- how it connects to earlier concepts
- what the student should master before moving on

### 3. Add multiple explanation modes
Support a few teaching styles:
- beginner mode: simple language and analogies
- standard mode: normal textbook explanation
- advanced mode: deeper detail and formal reasoning

### 4. Personalize the learning path
The system should adapt to the learner by:
- recommending easier review when performance is low
- skipping review when mastery is high
- suggesting the next best concept to study
- creating a study plan per student

### 5. Improve assessments
Add:
- short formative quizzes after each concept
- spaced repetition review
- confidence-based questions
- concept mastery tracking by topic rather than only by unit

## What will make the app better overall
- Better content structure: every lesson should have clear learning objectives, prerequisites, and summaries.
- Better pacing: the student should be guided through small concepts rather than long textbook chunks.
- Better personalization: the system should learn from each learner and adjust.
- Better usability: a cleaner dashboard for study plans, progress, and next steps.
- Better scalability: the same system should work for any textbook or curriculum, not just one course.

## Recommended implementation order
1. Add study-guide and module-breakdown fields to the generated lesson packs.
2. Show those modules in the web UI before the lesson begins.
3. Add beginner/standard/advanced explanation modes.
4. Track mastery by concept and generate personalized recommendations.
5. Add analytics and a stronger student dashboard.

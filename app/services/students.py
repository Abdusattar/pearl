from sqlalchemy.orm import Session
from app.models import Student


def get_next_free_pin(db: Session) -> str:
    """Наименьший свободный PIN 001-999 — не занят активным студентом."""
    used = {
        s.pin
        for s in db.query(Student.pin).filter(Student.status == "active").all()
    }
    for n in range(1, 1000):
        pin = f"{n:03d}"
        if pin not in used:
            return pin
    raise ValueError("Все PIN 001-999 заняты")


def get_student_by_pin(db: Session, pin: str) -> Student | None:
    return db.query(Student).filter(Student.pin == pin, Student.status == "active").first()


def deactivate_student(db: Session, student_id: int) -> Student:
    """Перевод в статус выбыл. PIN автоматически освобождается для нового ребёнка."""
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise ValueError(f"Студент {student_id} не найден")
    student.status = "inactive"
    db.flush()
    return student

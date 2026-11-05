
#include "AGVPathActor.h"

#include "Components/SplineComponent.h"
#include "Components/SplineMeshComponent.h"
#include "Engine/StaticMesh.h"
#include "UObject/ConstructorHelpers.h"

AAGVPathActor::AAGVPathActor()
{
    PrimaryActorTick.bCanEverTick = false;

    PathSpline = CreateDefaultSubobject<USplineComponent>(
        TEXT("PathSpline"));

    RootComponent = PathSpline;

    // 엔진 기본 Cube 메시 사용
    static ConstructorHelpers::FObjectFinder<UStaticMesh> MeshFinder(
        TEXT("/Engine/BasicShapes/Cube.Cube"));

    if (MeshFinder.Succeeded())
    {
        PathMesh = MeshFinder.Object;
    }
}

void AAGVPathActor::BuildPath(
    const TArray<FVector>& InPathPoints)
{
    if (!PathSpline || !PathMesh || InPathPoints.Num() < 2)
    {
        return;
    }

    // 기존 메시 제거
    for (USplineMeshComponent* Segment : PathSegments)
    {
        if (Segment)
        {
            Segment->DestroyComponent();
        }
    }

    PathSegments.Empty();
    PathSpline->ClearSplinePoints(false);

    // 전달받은 좌표는 월드 좌표
    for (const FVector& Point : InPathPoints)
    {
        PathSpline->AddSplinePoint(
            Point,
            ESplineCoordinateSpace::World,
            false);
    }

    PathSpline->UpdateSpline();

    // 각 구간마다 Spline Mesh 생성
    const int32 SegmentCount =
        PathSpline->GetNumberOfSplinePoints() - 1;

    for (int32 Index = 0; Index < SegmentCount; ++Index)
    {
        USplineMeshComponent* Segment =
            NewObject<USplineMeshComponent>(this);

        Segment->SetMobility(EComponentMobility::Movable);
        Segment->SetStaticMesh(PathMesh);
        Segment->SetForwardAxis(ESplineMeshAxis::X, false);

        Segment->SetupAttachment(PathSpline);
        Segment->RegisterComponent();

        FVector StartPosition;
        FVector StartTangent;
        FVector EndPosition;
        FVector EndTangent;

        PathSpline->GetLocalLocationAndTangentAtSplinePoint(
            Index,
            StartPosition,
            StartTangent);

        PathSpline->GetLocalLocationAndTangentAtSplinePoint(
            Index + 1,
            EndPosition,
            EndTangent);

        Segment->SetStartAndEnd(
            StartPosition,
            StartTangent,
            EndPosition,
            EndTangent,
            false);

        // Cube 기본 크기 100cm 기준
        Segment->SetStartScale(
            FVector2D(PathWidth / 100.0f,
                PathHeight / 100.0f),
            false);

        Segment->SetEndScale(
            FVector2D(PathWidth / 100.0f,
                PathHeight / 100.0f),
            true);

        Segment->SetCollisionEnabled(
            ECollisionEnabled::NoCollision);

        PathSegments.Add(Segment);
    }
}
